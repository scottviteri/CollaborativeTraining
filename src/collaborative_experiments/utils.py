import os
import torch
import sys
import time
from pathlib import Path
import json

from datasets import load_dataset
import accelerate
from llama import Llama, Tokenizer, ModelArgs, Transformer

from collaborative_experiments.constants import DEFAULT_MAX_CONTEXT_LENGTH, DEFAULT_MSG_CONTEXT_LENGTH


def get_device():
    """
    Get's either cuda, cpu, or mps, using accelerate
    """
    accelerator = accelerate.Accelerator()
    device = accelerator.device
    return device


def tile_a_tensor(reshaped_tensor):
    reshaped_tensor.fill_(50)  # shape (2, data_context_length)
    reshaped_tensor = reshaped_tensor[0:2]
    for i in range(reshaped_tensor.shape[1] // 3):
        reshaped_tensor[0, i * 3 + 1] += 1
        reshaped_tensor[1, i * 3 + 1] += 1
        reshaped_tensor[0, i * 3 + 2] += 2
        reshaped_tensor[1, i * 3 + 2] += 2
    return reshaped_tensor


def load_and_format_dataset(
    textbook_1_path, causal_lm_tokenizer, debug=False, reduced_data=0, train_context_length=DEFAULT_MAX_CONTEXT_LENGTH, msg_context_length=DEFAULT_MSG_CONTEXT_LENGTH
):
    """
    Takes input data as a string and then tokenizes it.
    Changes the size of each batch of data to be train_context_length - msg_context_length
    because msg_context_length will be prepended to the data getting us back to
    train_context_length

    Args:
        train_context_length (int): the length of the batch that we will train on
        msg_context_length (int): the length of the message that we will prepend to the data. Must be < train_context_length

    Returns:
        (torch.tensor): the data as a tensor of shape (n_batches, data_context_length) where data_context_length = train_context_length - msg_context_length
    """
    data_context_length = train_context_length - msg_context_length
    assert data_context_length > 0, f"train_context_length {train_context_length} must be greater than msg_context_length {msg_context_length}"
    dataset = load_dataset("text", data_files=textbook_1_path)
    print(dataset)

    # collapse dataset into one big string
    dataset_1 = dataset["train"]
    text = "\n".join(dataset_1["text"])

    # tokenize dataset
    dataset_1_tokenized = causal_lm_tokenizer(text, return_tensors="pt")

    # convert from shape (1, num_tokens) to (num_tokens/data_context_length, data_context_length)
    tokens_tensor = dataset_1_tokenized["input_ids"].squeeze()
    size = tokens_tensor.shape[0]
    size = (size // data_context_length) * (data_context_length)
    tokens_tensor = tokens_tensor[0:size]
    reshaped_tensor = tokens_tensor.view(-1, data_context_length)
    print(
        reshaped_tensor.shape
    )  # Should print torch.Size([num_tokens/data_context_length, data_context_length])
    # turn all values to be the same 11
    if debug:
        reshaped_tensor = tile_a_tensor(reshaped_tensor)
    elif reduced_data > 0:
        reshaped_tensor = reshaped_tensor[0:reduced_data]

    return reshaped_tensor


def load_llama_model(
    ckpt_dir: str = "../llama/llama-2-7b",
    tokenizer_path: str = "../llama/tokenizer.model",
    temperature: float = 0.6,
    top_p: float = 0.9,
    max_seq_len: int = 1024,
    max_gen_len: int = 64,
    max_batch_size: int = 8,
    device: str = "mps",
):
    os.environ["RANK"] = "0"
    # generator = Llama.build(
    #     ckpt_dir=ckpt_dir,
    #     tokenizer_path=tokenizer_path,
    #     max_seq_len=max_seq_len,
    #     max_batch_size=max_batch_size,
    # ) # fails for strange reasons
    # return generator.model, generator.tokenizer
    start_time = time.time()
    checkpoints = sorted(Path(ckpt_dir).glob("*.pth"))
    for chkpt_path in checkpoints:
        checkpoint = torch.load(chkpt_path, map_location="cpu")
    with open(Path(ckpt_dir) / "params.json", "r") as f:
        params = json.loads(f.read())

    model_args: ModelArgs = ModelArgs(
        max_seq_len=max_seq_len,
        max_batch_size=max_batch_size,
        **params,
    )
    tokenizer = Tokenizer(model_path=tokenizer_path)
    if device == "cuda":
        torch.set_default_tensor_type(torch.cuda.HalfTensor)
    elif device == "mps":
        torch.set_default_tensor_type(torch.HalfTensor)
    else:
        torch.set_default_tensor_type(torch.BFloat16Tensor)
    model = Transformer(model_args)
    model.load_state_dict(checkpoint, strict=False)
    print(f"Loaded in {time.time() - start_time:.2f} seconds")
    return model, tokenizer
