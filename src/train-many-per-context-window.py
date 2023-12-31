# pip install transformers datasets==2.14.6 torchtyping==0.1.4 && pip install peft einops apache_beam==2.51.0 matplotlib wandb && pip install -U flash-attn --no-build-isolation
# huggingface-cli login
"""  
Main Definitions:

1. sweep_config class instance
2. train function

High level structure:

1. Import necessary libraries and modules:  This includes libraries like torch, tqdm, einops, wandb, and your custom modules RaoConfig and RaoGenerator.

2. Define sweep configuration: This is a dictionary that contains the parameters for your training process, such as learning rate, batch size, number of batches, and model parameters. If you're using Weights & Biases (wandb), this configuration is used to set up a hyperparameter sweep.

3. Initialize wandb sweep: If you're using wandb, this line sets up a hyperparameter sweep with the given sweep configuration and project name.

4. Define the train function: This is the main function where the training process happens. It includes the following steps:

- Initialize wandb run: If you're using wandb, this step initializes a new run and sets up the configuration parameters for the run.

- Create RaoConfig instance: This step creates an instance of the RaoConfig class with the parameters from the sweep configuration.

- Create RaoGenerator instance: This step creates an instance of the RaoGenerator class, which is responsible for generating the reward-action-observation (RAO) triples.

- Initialize dataloader, loss function, and optimizer: This step initializes the dataloader for your dataset, the loss function for your model, and the optimizer for your training process.

- Training loop: This is the main loop where the training happens. For each batch in the dataloader, it generates a RAO tensor, calculates the loss, and updates the model parameters. If you're using wandb, it also logs the loss values to wandb.

5. Finish wandb run: If you're using wandb, this step finishes the current run after the training process is complete.

6. Start training process: This is the entry point of your program. If you're using wandb, it starts a wandb agent to run the train function. Otherwise, it directly calls the train function.
"""

import torch
from tqdm import tqdm
from einops import rearrange
import wandb
from rao_tools import RaoConfig
from rao_generator import RaoGenerator

sweep_config = {
    'method': 'grid', 
    'parameters': {
        'load_model': {'values': [False]},
        'use_wandb': {'values': [True]},  
        'model_name': {'values': ["distilgpt2"]},
        'lr': {'values': [1e-4]},
        'do_lora': {'values': [False]},
        'tok_p_loss': {'values': [9]},
        'tok_p_action': {'values': [30]},
        'tok_p_obs': {'values': [30]},
        'obs_p_doc': {"values": [5]},
        'normalize_to_ctxt_size': {'values': [True]},
        'impose_ctxt_size': {'values': [100]},
        'num_beams': {'values': [1]},
        'batch_size': {'values': [1]},
        'num_batches': {'values': [1000]},
        'interval_save_weights': {'values': [10]},
        'interval_print': {'values': [10]}
    }
}

sweep_id = wandb.sweep(sweep_config, project="collaborative-training-many-per-context-window")

def train():
    run = None
    if sweep_config['parameters']['use_wandb']['values'][0]:
        run = wandb.init(resume=sweep_config['parameters']['load_model']['values'][0])
        wb_cfg = run.config
        config_params = {param: getattr(wb_cfg, param) for param in sweep_config['parameters']}
    else:
        wb_cfg = None
        config_params = {param: sweep_config['parameters'][param]['values'][0] for param in sweep_config['parameters']}

    # fix obs_p_doc order
    cfg = RaoConfig(
        load_model=config_params['load_model'],
        wandb=sweep_config['parameters']['use_wandb']['values'][0],
        model_name=config_params['model_name'],
        lr = config_params['lr'],
        do_lora = config_params['do_lora'], 
        tok_p_loss=config_params['tok_p_loss'],
        tok_p_action=config_params['tok_p_action'],
        tok_p_obs=config_params['tok_p_obs'],
        obs_p_doc=config_params['obs_p_doc'],
        normalize_to_ctxt_size=config_params['normalize_to_ctxt_size'],
        impose_ctxt_size=config_params['impose_ctxt_size'],
        num_beams =config_params['num_beams'],
        batch_size=config_params['batch_size'],
        num_batches=config_params['num_batches'],
        interval_save_weights=config_params['interval_save_weights'],
        interval_print = config_params['interval_print'] 
    )
    # todo add flag for ld
    lora_string = "L" if cfg.do_lora else "nL"
    markov_string = "mkv_" if cfg.normalize_to_ctxt_size else ""
    if run is not None:
        run.name = f"{markov_string}ld_b{cfg.num_beams}_{lora_string}{cfg.model_name[:4]}_lr{cfg.lr}_rao{cfg.tok_p_loss}/{cfg.tok_p_action}/{cfg.tok_p_obs}_bs{cfg.batch_size}_nb{cfg.num_batches}"

    if not cfg.load_model:
        with open(f"saved_weights_and_losses/{cfg.model_name}", "w") as f:
            print("")

    NUM_DATAPOINTS = cfg.batch_size * cfg.num_batches if cfg.num_batches else None
    causal_lm = cfg.model
    causal_lm_tokenizer = cfg.tokenizer

    raogen = RaoGenerator(
        cfg=cfg,
        num_data_points=NUM_DATAPOINTS,
    )
    dataloader = raogen.dataloader

    average_loss_differences = []
    aggregate_losses = []
    loss_fn = torch.nn.CrossEntropyLoss(reduction="none")
    optimizer = torch.optim.Adam(causal_lm.parameters(), lr=cfg.lr)

    for batch_index, data in (
        tqdm(enumerate(dataloader), total=cfg.num_batches) if cfg.num_batches else tqdm(dataloader)
    ):
        if cfg.num_batches and batch_index > cfg.num_batches:
            break
        if batch_index > 0 and batch_index % cfg.interval_save_weights == 0:
            print(f"Saving trained_{cfg.model_name} \n\n")
            causal_lm_tokenizer.save_pretrained(cfg.path_2_tokenizer)
            causal_lm.save_pretrained(cfg.path_2_model)

        rao_tensor, new_loss_differences = raogen.gen_rao_tensor(
            data=data,
            optimizer=optimizer,
            loss_fn=loss_fn,
            average_loss_differences = average_loss_differences,
            aggregate_losses=aggregate_losses,
            batch_index=batch_index,
        )

        average_loss_differences.extend(new_loss_differences)

        for i in range(0, rao_tensor.shape[1], (cfg.ctxt_size // cfg.tok_p_rao)*cfg.tok_p_rao):
            rao_tensor_slice = rao_tensor[:, i:i+(cfg.ctxt_size // cfg.tok_p_rao)*cfg.tok_p_rao]
            rao_tensor_logits = causal_lm(rao_tensor_slice).logits[:, :-1, :]
            rao_tensor_loss = loss_fn(
                input=rearrange(
                    rao_tensor_logits,
                    "batch seq_length vocab_size -> batch vocab_size seq_length",
                ),
                target=rao_tensor_slice[:, 1:],
            )
            # Split rao_tensor_loss into loss_loss, action_loss, and observation_loss
            # rao_tensor.shape == (batch_size, num_tokens)
            with torch.no_grad():
                sections = rao_tensor_loss.split(cfg.tok_p_rao, dim=-1)
                rao_triples = [(section[:, :cfg.tok_p_loss], section[:, cfg.tok_p_loss:cfg.tok_p_loss+cfg.tok_p_action], section[:, cfg.tok_p_loss+cfg.tok_p_action:]) for section in sections]
                loss_loss, action_loss, observation_loss = zip(*rao_triples)
                loss_loss = torch.cat(loss_loss, dim=-1).mean()
                action_loss = torch.cat(action_loss,dim=-1).mean()
                observation_loss = torch.cat(observation_loss, dim=-1).mean()
                loss_weight = cfg.tok_p_loss / cfg.tok_p_rao 
                action_weight = cfg.tok_p_action / cfg.tok_p_rao 
                observation_weight = cfg.tok_p_obs / cfg.tok_p_rao
                if batch_index % cfg.interval_print == 0:
                    print(f"Loss/Action/Observation loss: {loss_loss}/{action_loss}/{observation_loss}")
                    print(f"Weighted Loss/Action/Observation loss: {loss_loss * loss_weight}/{action_loss * action_weight}/{observation_loss * observation_weight}")
            # Compute the mean of rao_tensor_loss and backward pass as usual
            aggregate_loss = rao_tensor_loss.mean()
            aggregate_losses.append(aggregate_loss.item())
            aggregate_loss.backward()
            print("Aggregate loss: ", aggregate_loss)
            # Calculate the relative weights for each loss component
            # Log the weighted components of the loss
            if wb_cfg:
                wandb.log({
                    "Aggregate loss": aggregate_loss,
                    "Weighted loss loss": loss_loss * loss_weight,
                    "Weighted action loss": action_loss * action_weight,
                    "Weighted observation loss": observation_loss * observation_weight
                })
            optimizer.step()

    if wb_cfg:
        run.finish()


if sweep_config['parameters']['use_wandb']['values'][0]:
    wandb.agent(sweep_id, function=train)
else:
    train()
