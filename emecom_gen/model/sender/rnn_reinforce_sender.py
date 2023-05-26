from torch import Tensor
from torch.nn import RNNCell, GRUCell, LSTMCell, Embedding, Linear, LayerNorm, Identity, Dropout
from torch.nn.parameter import Parameter
from torch.distributions import RelaxedOneHotCategorical
from torch.distributions import Categorical
from typing import Callable, Literal, Optional
import torch

from ...data import Batch
from .sender_output import SenderOutput, SenderOutputGumbelSoftmax
from .sender_base import SenderBase


def shape_keeping_argmax(x: Tensor) -> Tensor:
    return torch.zeros_like(x).scatter_(-1, x.argmax(dim=-1, keepdim=True), 1)


class ValueEstimator(Linear):
    def __init__(
        self,
        hidden_size: int,
    ) -> None:
        super().__init__(hidden_size, 1, bias=True)

    def forward(self, input: Tensor) -> Tensor:
        return super().forward(input).squeeze(-1)


class RnnReinforceSender(SenderBase):
    def __init__(
        self,
        object_encoder: Callable[[Tensor], Tensor],
        vocab_size: int,
        max_len: int,
        cell_type: Literal["rnn", "gru", "lstm"],
        embedding_dim: int,
        hidden_size: int,
        fix_message_length: bool,
        gs_temperature: float = 1,
        gs_straight_through: bool = True,
        enable_layer_norm: bool = True,
        dropout: float = 0,
    ) -> None:
        super().__init__()

        self.object_encoder = object_encoder
        self.vocab_size = vocab_size
        self.max_len = max_len
        self.fix_message_length = fix_message_length

        self.gs_temperature = gs_temperature
        self.gs_straight_through = gs_straight_through

        self.cell = {"rnn": RNNCell, "gru": GRUCell, "lstm": LSTMCell}[cell_type](
            embedding_dim,
            hidden_size,
        )
        self.embedding = Embedding(vocab_size, embedding_dim)
        self.bos_embedding = Parameter(torch.zeros(embedding_dim))
        self.hidden_to_output = Linear(hidden_size, vocab_size)
        self.value_estimator = ValueEstimator(hidden_size)

        if enable_layer_norm:
            self.layer_norm = LayerNorm(hidden_size, elementwise_affine=False)
        else:
            self.layer_norm = Identity()

        self.dropout = Dropout(p=dropout)

        self.reset_parameters()

    def reset_parameters(self):
        torch.nn.init.normal_(self.bos_embedding)

    def __call__(
        self,
        batch: Batch,
        forced_message: Optional[Tensor] = None,
    ) -> SenderOutput:
        return self.forward(
            batch,
            forced_message=forced_message,
        )

    def forward(
        self,
        batch: Batch,
        forced_message: Optional[Tensor] = None,
    ) -> SenderOutput:
        input = batch.input
        batch_size = input.shape[0]

        encoder_hidden_state = self.object_encoder(input)
        encoder_hidden_state = self.layer_norm.forward(encoder_hidden_state)

        h = encoder_hidden_state
        c = torch.zeros_like(h)
        e = self.bos_embedding.unsqueeze(0).expand(batch_size, *self.bos_embedding.shape)

        h_dropout_mask = self.dropout.forward(torch.ones_like(h))
        e_dropout_mask = self.dropout.forward(torch.ones_like(e))

        h = h * h_dropout_mask
        e = e * e_dropout_mask

        symbol_list: list[Tensor] = []
        logits_list: list[Tensor] = []
        estimated_value_list: list[Tensor] = []

        if forced_message is not None:
            num_steps = forced_message.shape[1]
        elif self.fix_message_length:
            num_steps = self.max_len
        else:
            num_steps = self.max_len - 1

        for step in range(num_steps):
            if isinstance(self.cell, LSTMCell):
                h, c = self.cell.forward(e, (h, c))
            else:
                h = self.cell.forward(e, h)

            h = h * h_dropout_mask
            h = self.layer_norm.forward(h)

            step_logits = self.hidden_to_output.forward(h)
            step_estimated_value = self.value_estimator.forward(h)

            if forced_message is not None:
                symbol = forced_message[:, step]
            if self.training:
                symbol = Categorical(logits=step_logits).sample()
            else:
                symbol = step_logits.argmax(dim=-1)

            e = self.embedding.forward(symbol)
            e = e * e_dropout_mask

            symbol_list.append(symbol)
            logits_list.append(step_logits)
            estimated_value_list.append(step_estimated_value)

        message = torch.stack(symbol_list, dim=1)
        logits = torch.stack(logits_list, dim=1)
        estimated_value = torch.stack(estimated_value_list, dim=1)

        if not self.fix_message_length:
            message = torch.cat([message, torch.zeros_like(message[:, -1:])], dim=1)
            logits = torch.cat([logits, torch.zeros_like(logits[:, -1:])], dim=1)
            estimated_value = torch.cat([estimated_value, torch.zeros_like(estimated_value[:, -1:])], dim=1)

        return SenderOutput(
            message=message,
            logits=logits,
            estimated_value=estimated_value,
            fix_message_length=self.fix_message_length,
            encoder_hidden_state=encoder_hidden_state,
        )

    def forward_gumbel_softmax(
        self,
        batch: Batch,
        forced_message: Optional[Tensor] = None,
    ) -> SenderOutputGumbelSoftmax:
        input = batch.input

        batch_size = input.shape[0]

        encoder_hidden_state = self.object_encoder(input)
        encoder_hidden_state = self.layer_norm.forward(encoder_hidden_state)

        h = encoder_hidden_state
        c = torch.zeros_like(h)
        e = self.bos_embedding.unsqueeze(0).expand(batch_size, *self.bos_embedding.shape)

        symbol_list: list[Tensor] = []
        logits_list: list[Tensor] = []

        if forced_message is not None:
            num_steps = forced_message.shape[1]
        elif self.fix_message_length:
            num_steps = self.max_len
        else:
            num_steps = self.max_len - 1

        for step in range(num_steps):
            if isinstance(self.cell, LSTMCell):
                h, c = self.cell.forward(e, (h, c))
            else:
                h = self.cell.forward(e, h)

            h = self.layer_norm.forward(h)

            step_logits = self.hidden_to_output.forward(h)

            if forced_message is not None:
                symbol = forced_message[:, step]
            elif self.training:
                symbol: Tensor = RelaxedOneHotCategorical(temperature=self.gs_temperature, logits=step_logits).rsample()
                if self.gs_straight_through:
                    symbol = symbol + (shape_keeping_argmax(symbol) - symbol).detach()
            else:
                symbol = shape_keeping_argmax(step_logits)

            symbol_list.append(symbol)
            logits_list.append(step_logits)

        message = torch.stack(symbol_list, dim=1)
        logits = torch.stack(logits_list, dim=1)

        if not self.fix_message_length:
            onehot_eos = torch.zeros_like(message[:, -1:])
            onehot_eos[:, 0, 0] = 1.0
            message = torch.cat([message, onehot_eos], dim=1)
            logits = torch.cat([logits, torch.zeros_like(logits[:, -1:])], dim=1)

        return SenderOutputGumbelSoftmax(
            message=message,
            logits=logits,
            fix_message_length=self.fix_message_length,
            straight_through=self.gs_straight_through,
            encoder_hidden_state=encoder_hidden_state,
        )
