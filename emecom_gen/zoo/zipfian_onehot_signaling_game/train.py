from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import Callback, ModelCheckpoint
from pytorch_lightning.loggers import CSVLogger, TensorBoardLogger
from torch.nn import Linear
from logzero import logger
from typing import Literal, Sequence
import json
import torch


from ...data import ZipfianOneHotDataModule
from ...metrics import DumpLanguage
from ...model.sender import RnnReinforceSender
from ...model.receiver import RnnReconstructiveReceiver
from ...model.message_prior import (
    MessagePriorBase,
    UniformMessagePrior,
    LengthExponentialMessagePrior,
    HiddenMarkovMessagePrior,
)
from ...model.game import (
    EnsembleBetaVAEGame,
    ConstantBetaScheduler,
    SigmoidBetaScheduler,
    AccuracyBasedBetaScheduler,
    InputDependentBaseline,
)
from ..common_argparser import CommonArgumentParser


class ArgumentParser(CommonArgumentParser):
    n_features: int = 1000
    exponent: float = -1  # Exponent of powerlaw distribution.

    max_len: int = 30
    vocab_size: int = 40
    fix_message_length: bool = False

    experiment_name: str = "zipfian-onehot-signaling-game"  # Name of sub-directory of `save_dir`.

    def process_args(self) -> None:
        if self.experiment_version == "":
            delimiter = "_"

            beta_scheduler_info: str = f"BETA{self.beta_scheduler_type}"
            match self.beta_scheduler_type:
                case "constant":
                    beta_scheduler_info += f"V{self.beta_constant_value}"
                case "sigmoid":
                    beta_scheduler_info += f"G{self.beta_sigmoid_gain}O{self.beta_sigmoid_offset}"
                case "acc-based":
                    beta_scheduler_info += f"E{self.beta_accbased_exponent}S{self.beta_accbased_smoothing_factor}"

            training_method_info = f"GS{self.gumbel_softmax_mode}"
            if self.gumbel_softmax_mode:
                pass
            else:
                training_method_info += f"BASELINE{self.baseline_type}{delimiter}NORM{self.reward_normalization_type}"

            sender_architecture_info = (
                f"SCELL{self.sender_cell_type}"
                f"H{self.sender_hidden_size}"
                f"E{self.sender_embedding_dim}"
                f"LN{self.sender_layer_norm}"
            )
            receiver_architecture_info = (
                f"RCELL{self.receiver_cell_type}"
                f"H{self.receiver_hidden_size}"
                f"E{self.receiver_embedding_dim}"
                f"LN{self.receiver_layer_norm}"
                f"IMPA{self.receiver_impatience}"
            )

            self.experiment_version = delimiter.join(
                [
                    f"FEATURE{self.n_features:0>4}",
                    f"VOC{self.vocab_size:0>4}",
                    f"LEN{self.max_len:0>4}",
                    f"POP{self.n_agent_pairs:0>4}",
                    f"PRIOR{self.prior_type}",
                    beta_scheduler_info,
                    training_method_info,
                    sender_architecture_info,
                    receiver_architecture_info,
                    f"SEED{self.random_seed:0>4}",
                ]
            )

        super().process_args()


def main():
    args = ArgumentParser().parse_args()

    args_save_path = args.save_dir / args.experiment_name / args.experiment_version / "args.json"
    args_save_path.parent.mkdir(parents=True, exist_ok=True)
    args.save(args_save_path.as_posix())

    logger.info(json.dumps(args, indent=4, default=repr))
    logger.info("Create Data Module.")

    datamodule = ZipfianOneHotDataModule(
        n_features=args.n_features,
        batch_size=args.batch_size,
        random_seed=args.random_seed,
        num_workers=args.num_workers,
        exponent=args.exponent,
        num_batches_per_epoch=args.accumulate_grad_batches,
    )

    logger.info("Creat Model")

    senders = [
        RnnReinforceSender(
            object_encoder=Linear(
                in_features=args.n_features,
                out_features=args.sender_hidden_size,
            ),
            vocab_size=args.vocab_size,
            max_len=args.max_len,
            cell_type=args.sender_cell_type,
            embedding_dim=args.sender_embedding_dim,
            hidden_size=args.sender_hidden_size,
            fix_message_length=False,
            enable_layer_norm=args.sender_layer_norm,
            enable_residual_connection=args.sender_residual_connection,
            dropout=args.sender_dropout,
        )
        for _ in range(args.n_agent_pairs)
    ]

    receivers = [
        RnnReconstructiveReceiver(
            object_decoder=Linear(
                in_features=args.receiver_hidden_size,
                out_features=args.n_features,
            ),
            vocab_size=args.vocab_size,
            cell_type=args.receiver_cell_type,
            embedding_dim=args.receiver_embedding_dim,
            hidden_size=args.receiver_hidden_size,
            enable_layer_norm=args.receiver_layer_norm,
            enable_residual_connection=args.receiver_residual_connection,
            enable_symbol_prediction=(args.prior_type == "receiver"),
            enable_object_logits_cumsum=args.receiver_object_logits_cumsum,
            dropout=args.receiver_dropout,
        )
        for _ in range(args.n_agent_pairs)
    ]

    priors: Sequence[MessagePriorBase | Literal["receiver"]]
    match args.prior_type:
        case "uniform":
            priors = [
                UniformMessagePrior(
                    vocab_size=args.vocab_size,
                    max_len=args.max_len,
                )
                for _ in range(args.n_agent_pairs)
            ]
        case "length-exponential":
            priors = [
                LengthExponentialMessagePrior(
                    vocab_size=args.vocab_size,
                    max_len=args.max_len,
                    base=args.length_exponential_prior_base,
                )
                for _ in range(args.n_agent_pairs)
            ]
            if args.length_exponential_prior_base == 1:
                logger.warning(
                    "`args.prior_type == 'length-exponential'` while `args.length_exponential_base == 1`. "
                    "It is essentially the same as `args.prior_type == uniform`."
                )
        case "hmm":
            priors = [
                HiddenMarkovMessagePrior(
                    n_hidden_states=args.hmm_prior_num_hidden_states,
                    n_observable_states=args.vocab_size,
                )
                for _ in range(args.n_agent_pairs)
            ]
        case "receiver":
            priors = ["receiver" for _ in range(args.n_agent_pairs)]

    match args.beta_scheduler_type:
        case "constant":
            beta_scheduler = ConstantBetaScheduler(args.beta_constant_value)
        case "sigmoid":
            beta_scheduler = SigmoidBetaScheduler(args.beta_sigmoid_gain, args.beta_sigmoid_offset)
        case "acc-based":
            beta_scheduler = AccuracyBasedBetaScheduler(
                args.beta_accbased_exponent, args.beta_accbased_smoothing_factor
            )

    match args.baseline_type:
        case "input-dependent":
            baseline = InputDependentBaseline(
                object_encoder=Linear(args.n_features, args.sender_hidden_size),
                vocab_size=args.vocab_size,
                cell_type=args.sender_cell_type,
                embedding_dim=args.sender_embedding_dim,
                hidden_size=args.sender_hidden_size,
                num_senders=args.n_agent_pairs,
                num_receivers=args.n_agent_pairs,
                enable_layer_norm=args.sender_layer_norm,
                enable_residual_connection=args.sender_residual_connection,
                dropout=args.sender_dropout,
            )
        case literal:
            baseline = literal

    model = EnsembleBetaVAEGame(
        senders=senders,
        receivers=receivers,
        priors=priors,
        sender_lr=args.sender_lr,
        receiver_lr=args.receiver_lr,
        sender_weight_decay=args.sender_weight_decay,
        receiver_weight_decay=args.receiver_weight_decay,
        beta_scheduler=beta_scheduler,
        baseline=baseline,
        reward_normalization_type=args.reward_normalization_type,
        optimizer_class=args.optimizer_class,
        num_warmup_steps=args.num_warmup_steps,
        sender_update_prob=args.sender_update_prob,
        receiver_update_prob=args.receiver_update_prob,
        prior_update_prob=args.prior_update_prob,
        gumbel_softmax_mode=args.gumbel_softmax_mode,
        receiver_impatience=args.receiver_impatience,
        receiver_incrementality=args.receiver_incrementality,
        accumulate_grad_batches=args.accumulate_grad_batches,
    )

    logger.info("Create a trainer")

    callbacks: list[Callback] = [
        ModelCheckpoint(
            dirpath=args.save_dir / args.experiment_name / args.experiment_version,
            every_n_epochs=args.save_checkpoint_every,
        ),
        DumpLanguage(
            save_dir=args.save_dir / args.experiment_name / args.experiment_version,
            meaning_type="target_label",
        ),
    ]

    trainer = Trainer(
        logger=[
            CSVLogger(
                save_dir=args.save_dir,
                name=args.experiment_name,
                version=args.experiment_version,
            ),
            TensorBoardLogger(
                save_dir=args.save_dir,
                name=args.experiment_name,
                version=args.experiment_version,
            ),
        ],
        callbacks=callbacks,
        accelerator=args.accelerator,
        devices=args.devices,
        max_epochs=args.n_epochs,
        check_val_every_n_epoch=args.check_val_every_n_epoch,
        enable_progress_bar=args.enable_progress_bar,
    )

    logger.info("Start fitting.")

    torch.set_float32_matmul_precision("high")
    trainer.fit(model=model, datamodule=datamodule)


if __name__ == "__main__":
    main()
