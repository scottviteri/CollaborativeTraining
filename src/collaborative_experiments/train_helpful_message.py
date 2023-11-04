"""
This is a file where we train a model to send more helpful messages to another model.

We will just simply use expert iteration to train the model to say more of the things that
created a more helpful
"""
import os
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import LoraConfig, get_peft_model

import torch
from torchtyping import TensorType, patch_typeguard
from typeguard import typechecked
from dataclasses import dataclass
import fire
import wandb
import pandas as pd
from tqdm import tqdm

from collaborative_experiments.mvp_loss_decrease import (
    create_model_helpful_message,
    msg_loss,
)

from collaborative_experiments.utils import (
    get_device,
    load_and_format_dataset,
    tile_a_tensor,
)
from collaborative_experiments.constants import (
    DEFAULT_MSG_CONTEXT_LENGTH,
    DEFAULT_MAX_CONTEXT_LENGTH,
)
from collaborative_experiments.mocking import mockCausalGPT2

LOG_COLUMNS = ["step", "loss", "helpful_msg", "original_text"]#, "logits_shifted", "shifted_model_input", "model_input"]

@dataclass
class TrainConfig:
    experiment_name: str = "train-helpful-message-via-expert-iteration"
    messages_per_datapoint: int = 2
    datapoints_per_batch: int = 1
    model_name: str = "distilgpt2"
    debug_dataset_size: int = 10
    data_file_path: str = "data/st_patrick_biography.txt"
    training_context_length: int = 256
    helpful_msg_context_length: int = 64
    epochs: int = 1
    wandb: bool = True
    device: str = "cpu"  # mps
    lr: float = 5e-5
    verbose: bool = False
    do_lora: bool = True
    lora_rank: int = None


def generate_msg_data_pairs(
    data_loader: torch.utils.data.DataLoader,
    msg_context_length: int,
    causal_lm: torch.nn.Module,
    causal_lm_tokenizer: AutoTokenizer,
    messages_per_datapoint: int = 1,
    num_data_to_use: int = 2,
) -> list:
    data_msg_pairs = []
    for i, original_text in enumerate(data_loader):
        if i == num_data_to_use:
            break
        for _ in range(messages_per_datapoint):
            with torch.no_grad():  # ensures no gradient info is saved
                helpful_msg = create_model_helpful_message(
                    original_text,
                    causal_lm_tokenizer,
                    causal_lm,
                    max_helpful_message_length=msg_context_length,
                ).to(causal_lm.device)
            original_text = original_text.to(causal_lm.device)
            # print("Generated message")
            # print(msg)
            example = {
                "original_text": original_text,
                "helpful_msg": helpful_msg,
            }
            data_msg_pairs.append(example)
    return data_msg_pairs


def log_row_fn(example: dict) -> list:
    new_example = []
    for key in LOG_COLUMNS:
        value = example[key]
        if key == "loss":
            value = str(value.item())
        elif isinstance(value, torch.Tensor):
            value = value.clone().detach().cpu().numpy()
        new_example.append(value)
    return new_example


def train_step(
    data_loader: torch.utils.data.DataLoader,
    msg_context_length: int,
    causal_lm: torch.nn.Module,
    causal_lm_tokenizer: AutoTokenizer,
    device: torch.device,
    loss_fn: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    messages_per_datapoint: int,
    scheduler: torch.optim.lr_scheduler._LRScheduler = None,
    num_data_to_use: int = 1,
    logging=True,
    step: int = -1
) -> bool:

    if num_data_to_use != 1:
        raise NotImplementedError(
            "num_data_to_use (a.k.a. datapoints_per_batch) must be 1 for now"
        )
    data_msg_pairs = generate_msg_data_pairs(
        data_loader,
        msg_context_length,
        causal_lm,
        causal_lm_tokenizer,
        messages_per_datapoint=messages_per_datapoint,
        num_data_to_use=num_data_to_use,
    )
    if len(data_msg_pairs) == 0:
        return True
   
    log_table = []
    for example in data_msg_pairs:
        original_text = example["original_text"]#.to(causal_lm.device)
        helpful_msg = example["helpful_msg"]#.to(causal_lm.device)

        loss, logits_shifted, shifted_model_input, model_input = msg_loss(
            original_text, helpful_msg, causal_lm, loss_fn, device
        )
        example["loss"] = loss
        # print("Loss was", loss)
        if logging:
            #wandb.log({"step": step, "loss": loss, "helpful_msg": helpful_msg, "original_text": original_text})
            log_table_entry = {}
            log_table_entry["step"] = step
            log_table_entry["loss"] = loss
            log_table_entry["helpful_msg"] = causal_lm_tokenizer.decode(helpful_msg[0])
            log_table_entry["original_text"] = causal_lm_tokenizer.decode(original_text[0])
            #log_table_entry["eval_mode_loss"] = best_example["loss"].item()
            #log_table_entry["train_mode_loss"] = loss.item()
            log_table_entry["logits_shifted"] = logits_shifted
            log_table_entry["shifted_model_input"] = shifted_model_input
            log_table_entry["model_input"] = model_input
            log_table.append(log_table_entry)
            #print("step: ", step)
            #log_row = log_row_fn(new_example)
            #log_table.add_data(*log_row)
    # 3. Rank the better ones, (also log all the losses in case we want to do RLHF eventually)
    data_msg_pairs.sort(key=lambda x: x["loss"])
    # fine tune on the best one
    best_example = data_msg_pairs[0]
    original_text = best_example["original_text"]
    helpful_msg = best_example["helpful_msg"]

    causal_lm.train()
    loss, logits_shifted, shifted_model_input, model_input = msg_loss(
        original_text, helpful_msg, causal_lm, loss_fn, device, requires_grad=True
    )

    # print(f"Auto regressive loss on helpful_msg was {loss.item()}")
    loss.backward()
    optimizer.step()
    optimizer.zero_grad()
    if scheduler:
        scheduler.step()
    #wandb.log(**log_dict)
    #if log_table is not None: 
    #    wandb.log({"log_table": log_table, **log_dict}, commit=True)
    return False, log_table if logging else None


def train(cfg: TrainConfig):
    if cfg.wandb:
        wandb.init(project="collaborative_training", config=cfg)

    device = get_device(cfg.device)
    if cfg.model_name == "gpt-neo":
        causal_lm = AutoModelForCausalLM.from_pretrained("EleutherAI/gpt-neo-2.7B")
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-neo-2.7B")
    elif cfg.model_name == "gpt-j":
        causal_lm = AutoModelForCausalLM.from_pretrained("EleutherAI/gpt-j-6b")
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-j-6b")
    elif cfg.model_name == "gpt2":
        causal_lm = AutoModelForCausalLM.from_pretrained("gpt2")
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("gpt2")
    elif cfg.model_name == "gpt2-medium":
        causal_lm = AutoModelForCausalLM.from_pretrained("gpt2-medium")
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("gpt2-medium")
    elif cfg.model_name == "gpt2-large":
        causal_lm = AutoModelForCausalLM.from_pretrained("gpt2-large")
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("gpt2-large")
    elif cfg.model_name == "mock":
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("distilgpt2")
        causal_lm = mockCausalGPT2(causal_lm_tokenizer)
    else:
        causal_lm = AutoModelForCausalLM.from_pretrained("distilgpt2")
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("distilgpt2")
    if cfg.verbose:
        print("Loaded causal LM")
    if cfg.verbose:
        print(causal_lm)
    causal_lm = causal_lm.to(device)
    # We are setting this token to be eos, so we must make sure to use attention masks
    # to not attend to these positions.
    causal_lm_tokenizer.pad_token_id = causal_lm_tokenizer.eos_token_id
    if cfg.verbose:
        print("Loaded causal LM to device")

    if cfg.do_lora:
        if cfg.lora_rank is None:
            lrank = 16
        else:
            lrank = cfg.lora_rank
        lora_config = LoraConfig(
            r=lrank,
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=["c_attn", "c_fc"],
        )
        causal_lm = get_peft_model(causal_lm, lora_config)

    print(causal_lm)

    # load dataset
    # https://www.gutenberg.org/ebooks/71431
    current_path = os.path.dirname(os.path.realpath(__file__))
    # textbook_1_path = os.path.join(current_path, "../../", cfg.data_file_path)
    data_loader, seq_len = load_and_format_dataset(
        cfg.data_file_path,
        causal_lm_tokenizer,
        # debug=debug,
        debug_dataset_size=cfg.debug_dataset_size,
        training_context_length=cfg.training_context_length,
    )
    loss_fn = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(causal_lm.parameters(), lr=cfg.lr)

    log_table = wandb.Table(columns=LOG_COLUMNS)
    table = []
    for step in tqdm(range(cfg.epochs), desc="Epochs"):
        finished, epoch_log_table = train_step(
            data_loader,
            cfg.helpful_msg_context_length,
            causal_lm,
            causal_lm_tokenizer,
            device,
            loss_fn,
            optimizer,
            messages_per_datapoint=cfg.messages_per_datapoint,
            num_data_to_use=cfg.datapoints_per_batch,
            logging=True,
            step=step
        )
        for row in epoch_log_table:
            log_table.add_data(*log_row_fn(row))
            table.append(log_row_fn(row))
        #log_table.add_data([log_row_fn(row) for row in epoch_log_table])
            #wandb.log({"log_table": log_row_fn(r) for r in log_table})
        best_example = sorted(epoch_log_table, key=lambda x: x["loss"])[0]
        wandb.log({"loss": best_example["loss"]})
        if finished:
            if cfg.verbose:
                print(f"There is no more data to use. Stopping at step {step}")
            break
    wandb.log({"log_table": log_table})
    print("done")


def main(
    messages_per_datapoint: int = 4,
    datapoints_per_batch: int = 1,
    model_name: str = "distilgpt2",  # "gpt2-medium", # "gpt-neo", # "distilgpt2",
    debug_dataset_size: int = 10,
    data_file_path: str = "data/st_patrick_biography.txt",
    training_context_length: int = 64,
    helpful_msg_context_length: int = 64,
    epochs: int = 10,
) -> bool:
    """
    Args:
        sample size (int): the number of helpful messages to generate per original text
        datapoints_per_batch (int): the number of original texts to use per batch
    """

    cfg = TrainConfig(
        messages_per_datapoint=messages_per_datapoint,
        datapoints_per_batch=datapoints_per_batch,
        model_name=model_name,
        debug_dataset_size=debug_dataset_size,
        data_file_path=data_file_path,
        training_context_length=training_context_length,
        helpful_msg_context_length=helpful_msg_context_length,
        epochs=epochs,
        device="mps",  # mps
        do_lora=True,
        lora_rank=16,
    )
    train(cfg)
    return True


if __name__ == "__main__":
    fire.Fire(main)
