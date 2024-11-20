/root/.local/bin/poetry run python -m emecom_gen.zoo.attval_signaling_game.train \
    --vocab_size 9 \
    --max_len 32 \
    --fix_message_length false \
    --n_epochs 20000 \
    --batch_size 2048 \
    --sender_cell_type gru \
    --sender_hidden_size 512 \
    --sender_embedding_dim 32 \
    --sender_layer_norm true \
    --receiver_cell_type gru \
    --receiver_hidden_size 512 \
    --receiver_embedding_dim 32 \
    --receiver_layer_norm true \
    --receiver_dropout_alpha 1e-3 \
    --baseline_type baseline-from-sender \
    --beta_scheduler_type rewo \
    --beta_rewo_communication_loss_constraint 0.3 \
    --prior_type receiver \
    --n_attributes 4 \
    --n_values 4 \
    --random_seed 1