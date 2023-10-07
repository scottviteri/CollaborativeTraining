"""
for llama use
```
torchrun --nproc_per_node 1 src/collaborative_experiments/mvp_loss_decrease.py
```
# Experiment outline:
## Goal:
    1. Measure a decrease in the loss of a model on a particular high quality dataset 
        when a helpful message is prepended to the prompt.
## Hypothesis:
    1. There are strings you can pre-pend to a message that will decrease the loss of the model
    2. Downstream hypothesis if this works: We can see if a LM is able to figure out how to provide such helpful and honest messages.
## Method:
    1. Train a model on a dataset
    2. Measure the loss of the model on each batch of the dataset with and without a prompt
## Evaluation and Experiments:
    1. Look for potential complications by plotting per token loss as a function of token position.
    2. Evaluate null hypothesis: just a random sentence prepended of a fixed length.
    3. Evaluate hypotheses: a) The first sentence from the batch. b) The first sentence wrapped in a message explaining this is a
        helpful message. c) A model generated summary of the sentences. - Helpful message generated by gpt4 as upper bound on competence
"""

import os
import torch
import torch.nn.functional as F
from transformers import AutoModelForCausalLM, AutoTokenizer
from tqdm import tqdm
import plotly.express as px
import pandas as pd
import numpy as np
import openai
import wandb

from torchtyping import TensorType, patch_typeguard
from typeguard import typechecked
from transformers import PreTrainedTokenizerFast
from transformers.models.auto.modeling_auto import AutoModelForCausalLM
from typing import *

# from jaxtyping import Array, Int, Float

from concurrent.futures import ThreadPoolExecutor
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
    retry_if_exception_type,
)

from collaborative_experiments.constants import (
    DEFAULT_MAX_CONTEXT_LENGTH,
    DEFAULT_MSG_CONTEXT_LENGTH,
)
from collaborative_experiments.utils import (
    get_device,
    load_and_format_dataset,
    load_llama_model,
)
from collaborative_experiments.mocking import mockCausalGPT2

import logging

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LOGGING_DICT_WANDB = {}


class OpenAIException(Exception):
    """
    Custom exception to make sure we only retry due to errors from OpenAI
    and not on other errors.
    """

    def __init__(self, Exception):
        self.Exception = Exception
        self.message = str(Exception)


class ExperimentConfig:
    def __init__(self, msg_fn, expected_length, name):
        self.msg_fn = msg_fn
        self.expected_length = expected_length  # measured in number of tokens
        self.name = name


def create_helpful_message_1(tokens, tokens_to_grab=DEFAULT_MSG_CONTEXT_LENGTH):
    """
    Returns the first tokens_to_grab tokens of the input tokens
    """
    msg = tokens[:, :tokens_to_grab]
    return msg


def create_model_helpful_message(
    uncompressed_tokens: TensorType["batch", "seq_len"],
    causal_lm_tokenizer: PreTrainedTokenizerFast,
    causal_lm: AutoModelForCausalLM,
    custom_user_prompt: Optional[str] = None,
    max_helpful_message_length: int = DEFAULT_MSG_CONTEXT_LENGTH,
) -> TensorType["batch", "seq_len"]:
    """
    Creates a helpful message using the causal language model

    Args:
        uncompressed_tokens (TensorType): The input tokens
        causal_lm_tokenizer (PreTrainedTokenizerFast): The tokenizer used by the causal language model
        causal_lm (AutoModelForCausalLM): The causal language model
        custom_user_prompt (Optional[str]): Custom user prompt. If None, a default prompt is used
        max_helpful_message_length (int): The maximum length of the helpful message

    Returns:
        TensorType: The helpful message
    """
    text = causal_lm_tokenizer.decode(uncompressed_tokens[0])
    if custom_user_prompt is None:
        custom_user_prompt = "You are a language model's assistant, and your job is to prepend text that makes the following text as predictable as possible. Do not be afraid to copy surprising parts of the text verbatim. <Begin Text-to-Summarize> "
    custom_user_prompt += text + "</End Text-To-Summarize> <Begin Summarization> "
    converted_tokens = (
        causal_lm_tokenizer.encode(custom_user_prompt, return_tensors="pt")
        .to(causal_lm.device)
        .to(torch.int32)
    )
    # Ensure the number of tokens in the message does not exceed the model's maximum position embeddings
    assert (
        converted_tokens.shape[1] + max_helpful_message_length
        <= causal_lm.config.n_positions
    ), "The total number of tokens exceeds the model's maximum position embeddings"

    seq_len = converted_tokens.shape[1]
    # only return new tokens
    # causal_lm = causal_lm.to("cpu")
    helpful_message = causal_lm.generate(
        # input_ids=converted_tokens.to("cpu"),
        input_ids=converted_tokens,
        max_new_tokens=max_helpful_message_length,
        do_sample=True,
        top_p=0.90,
        temperature=0.7,
        num_return_sequences=1,
    )[:, seq_len:]
    assert (
        helpful_message.shape[1] <= max_helpful_message_length
    ), "somehow the message is longer than the max length"
    if helpful_message.shape[1] < max_helpful_message_length:
        padding_length = max_helpful_message_length - helpful_message.shape[1]
        helpful_message = pad_msg(
            helpful_message, padding_length, causal_lm_tokenizer.encode("-")[0]
        )
    return helpful_message


def pad_msg(msg_tokens, pad_length, pad_token_id=0):
    padding = torch.full((1, pad_length), pad_token_id, device=msg_tokens.device)
    msg_tokens = torch.cat([padding, msg_tokens], dim=1)
    return msg_tokens


@retry(
    wait=wait_random_exponential(max=10),
    stop=stop_after_attempt(2),
    retry=retry_if_exception_type(OpenAIException),
)
def create_openai_helpful_message(
    tokens,
    causal_lm_tokenizer,
    system_prompt=None,
    user_prompt=None,
    print_msg=False,
    msg_context_length=DEFAULT_MSG_CONTEXT_LENGTH,
):
    print("trying to create openai helpful message")
    # Convert tokens to text
    text = causal_lm_tokenizer.decode(tokens[0])
    # Make a chat completion call to GPT-3.5
    if system_prompt is None:
        system_prompt = "You are a language model's assistant, and your job is to make 'prepend text that makes the following text as predictable as possible. Do not be afraid to copy surprising parts of the text verbatim."
    if user_prompt is None:
        user_prompt = "Please generate a prepend string for the following text: "
    user_prompt += text
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
        )
    except Exception as e:
        raise OpenAIException(e)
    # Get the prepend string from the response
    prepend_string = response.choices[0].message["content"]
    # Convert the summary back to tokens
    msg_tokens = causal_lm_tokenizer.encode(prepend_string, return_tensors="pt")
    # Decode the summary tokens for printing
    decoded_main_tokens = causal_lm_tokenizer.decode(
        tokens[:, : -msg_tokens.shape[1]][0]
    )
    if print_msg:
        print(
            "Prepend string: ", prepend_string, "\nMain string: ", decoded_main_tokens
        )
    if msg_tokens.shape[1] > msg_context_length:
        msg_tokens = msg_tokens[:, :msg_context_length]
    if msg_tokens.shape[1] < msg_context_length:
        padding_length = msg_context_length - msg_tokens.shape[1]
        msg_tokens = pad_msg(
            msg_tokens, padding_length, causal_lm_tokenizer.encode("-")[0]
        )
    return msg_tokens


def msg_loss(content, msg, causal_lm, loss_fn, device, requires_grad=False):
    msg_length = msg.shape[1]
    # Get the logits for content
    model_input = torch.cat((msg, content), dim=1)
    model_input = model_input.to(device)
    outputs_original = causal_lm(input_ids=model_input)
    logits = outputs_original.logits
    if not requires_grad:
        logits = logits.detach()
    logits_shifted = logits[
        :, msg_length:-1, :
    ]  # negative one because prediction shifts things by one
    # should be shape (b, data_context_length - 1, vocab_size)

    # now create one hot labels to get the loss with.
    shifted_model_input = model_input[
        :, msg_length + 1 :
    ]  # we shift 1 more because the logits predict one in the future
    labels = torch.nn.functional.one_hot(
        shifted_model_input, num_classes=causal_lm.config.vocab_size
    ).to(torch.float32)
    loss = loss_fn(logits_shifted[:,], labels)  # only calculate loss on content

    return loss, logits_shifted, shifted_model_input, model_input


def train_step(
    batch,
    causal_lm,
    loss_fn,
    device,
    verbose=False,
    debug=False,
    pytest=False,
):
    """
    Args:
        batch (dict): a dictionary with a 'msg' key and a 'content' key. The 'msg' key is a tensor of shape (batch_size, msg_context_length)
            and the 'content' key is a tensor of shape (batch_size, data_context_length)
    Returns:
        loss (torch.tensor): a tensor of shape (batch_size, data_context_length - 1)
        correct_probs (torch.tensor): a tensor of shape (batch_size, data_context_length - 1)
        if pytest, returns logits_shifted (torch.tensor): a tensor of shape (batch_size, data_context_length - 1, vocab_size)
    """
    msg = batch["msg"]  # shape (batch_size, msg_context_length)
    content = batch["content"]  # shape (batch_size, data_context_length)
    loss, logits_shifted, shifted_model_input, model_input = msg_loss(
        content, msg, causal_lm, loss_fn, device
    )

    if verbose:
        tqdm.write(f"{loss}")

    # Compute softmax over the last dimension to get probabilities
    probs = F.softmax(
        logits_shifted, dim=-1
    )  # shape (model_input_size, seq_len, vocab_size)

    # Use gather to pick the probabilities corresponding to the correct token at each position
    correct_probs = (
        probs.gather(-1, shifted_model_input.unsqueeze(-1)).squeeze(-1).detach()
    )  # shape (model_input_size, seq_len)
    if debug:
        print("model_input: ", model_input)
        print("correct probs: ", correct_probs)
        # get one sentence
        # print the tokens that it assigns max probability to
        # print the actual sentence
        sentence_tokens = model_input[0]
        sentence_probs = probs[0]
        sentence_correct_probs = correct_probs[0]
    if pytest:
        return loss, correct_probs, logits_shifted
    return loss.to("cpu"), correct_probs


def batched_create_openai_msgs(dataset_1_loader, config):
    all_batches = []
    for batch in tqdm(dataset_1_loader, desc=f"Experiment {config.name}"):
        all_batches.extend(batch)
    all_batches_dataloader = torch.utils.data.DataLoader(
        all_batches, batch_size=1, shuffle=False
    )
    with ThreadPoolExecutor(max_workers=10) as executor:
        messages_batched = list(
            tqdm(
                executor.map(config.msg_fn, all_batches_dataloader, chunksize=1),
                total=len(all_batches),
                desc=f"Reformating exp {config.name} for multi-threading",
            )
        )
    return messages_batched


def run_experiment(
    config,
    dataset_1_loader,
    causal_lm,
    loss_fn,
    device,
    batched_openai=True,
    verbose=False,
):
    # we subtract one to the size because causal_lm will predict the next token
    # therefore we can only check predictions for the first expected_length - 1 tokens
    correct_probs_all = torch.zeros(config.expected_length - 1).to(device)
    losses = []

    if batched_openai and "openai" in config.name:
        messages_batched = iter(batched_create_openai_msgs(dataset_1_loader, config))
        # function is now an iterator over messages_batched, ignores the batch
        config.msg_fn = lambda x: messages_batched.__next__()

    if config.name == "original":
        LOGGING_DICT_WANDB[f"{config.name}_content"] = []
    else:
        LOGGING_DICT_WANDB[f"{config.name}_msg_decoded"] = []
    for i, batch in enumerate(tqdm(dataset_1_loader, desc=f"Experiment {config.name}")):
        batch_dict = {}
        batch_dict["msg"] = config.msg_fn(batch)
        tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-neo-2.7B")
        helpful_msg_decoded = tokenizer.decode(batch_dict["msg"][0])
        if verbose:
            tqdm.write("msg: " + helpful_msg_decoded)
            tqdm.write(f"---end-helpful-msg-{config.name}--")
            tqdm.write("content: " + tokenizer.decode(batch[0]))
            tqdm.write(f"---end-content--{config.name}--")
        if config.name == "original":
            LOGGING_DICT_WANDB[f"{config.name}_content"].append(
                tokenizer.decode(batch[0])
            )
        else:
            LOGGING_DICT_WANDB[f"{config.name}_msg_decoded"].append(helpful_msg_decoded)

        batch_dict["content"] = batch
        loss, correct_probs = train_step(
            batch=batch_dict, causal_lm=causal_lm, loss_fn=loss_fn, device=device
        )
        correct_probs_all += correct_probs.mean(dim=0)
        wandb.log({f"{config.name}_loss": loss.item()}, commit=False)
        wandb.log({f"{config.name}_correct_probs": correct_probs.mean(dim=0)})
        losses.append(loss)

    return losses, correct_probs_all.to("cpu")


def main(
    save_dir="results_debug",
    debug=False,
    BATCH_SIZE=1,
    model_name="distilgpt2",
    reduced_data=10,
    train_context_length=DEFAULT_MAX_CONTEXT_LENGTH,
    msg_context_length=DEFAULT_MSG_CONTEXT_LENGTH,
    list_of_experiments="all",
    data_file_path="data/st_patrick_biography.txt",
    batched_openai=True,
    verbose=True,
):
    if BATCH_SIZE != 1:
        raise NotImplementedError(
            "Only implemented for batch size 1, not {}".format(BATCH_SIZE)
        )
    if "mock" in model_name:
        os.environ["WANDB_MODE"] = "dryrun"
    else:
        os.environ["WANDB_MODE"] = "online"
    wandb.init(
        project="collaborative_training",
        config={
            "run_finished_succesfully": False,
            "model_name": model_name,
            "save_dir": save_dir,
            "list_of_experiments": list_of_experiments,
            "reduced_data": reduced_data,
            "train_context_length": train_context_length,
            "msg_context_length": msg_context_length,
            "batch_size": BATCH_SIZE,
            "debug": debug,
            "data_file_path": data_file_path,
            "batched_openai": batched_openai,
            "verbose": verbose,
        },
    )
    device = get_device(model_name)
    if model_name == "llama":
        causal_lm, causal_lm_tokenizer = load_llama_model(device=device)
    elif model_name == "gpt-neo":
        causal_lm = AutoModelForCausalLM.from_pretrained("EleutherAI/gpt-neo-2.7B")
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-neo-2.7B")
    elif model_name == "mock":
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("distilgpt2")
        causal_lm = mockCausalGPT2(causal_lm_tokenizer)
    else:
        causal_lm = AutoModelForCausalLM.from_pretrained("distilgpt2")
        causal_lm_tokenizer = AutoTokenizer.from_pretrained("distilgpt2")
    print("Loaded causal LM")
    print(causal_lm)
    causal_lm = causal_lm.to(device)
    # We are setting this token to be eos, so we must make sure to use attention masks
    # to not attend to these positions.
    causal_lm_tokenizer.pad_token_id = causal_lm_tokenizer.eos_token_id
    print("Loaded causal LM to device")

    # load dataset
    # https://www.gutenberg.org/ebooks/71431
    current_path = os.path.dirname(os.path.realpath(__file__))
    textbook_1_path = os.path.join(current_path, "../../", data_file_path)
    dataset_1_loader, seq_len = load_and_format_dataset(
        textbook_1_path,
        causal_lm_tokenizer,
        debug=debug,
        reduced_data=reduced_data,
        train_context_length=train_context_length,
    )
    ## make a pytorch data loader for the dataset

    loss_fn = torch.nn.CrossEntropyLoss()

    experiments = []
    # user_prompt = "Your job is to compress the following text, such that you can reconstruct it later. Do not worry about human legibility and you are allowed to use unicode. Finish with </End compressed text> <example>This is called a covariant transformation law, because the covector components transform by the same matrix as the change of basis matrix. The components of a more general tensor are transformed by some combination of covariant and contravariant transformations, with one transformation law for each index. If the transformation matrix of an index is the inverse matrix of the basis transformation, then the index is called contravariant and is conventionally denoted with an upper index (superscript). If the transformation matrix of an index is the basis transformation itself, then the index is called covariant and is denoted with a lower index (subscript).CovTransLaw:covector=ΔbasisMat. Tensor=comb(cov&contra); 1law/idx. InvMat=basisTrans→contra&↑. BasisTrans=cov&↓</example><Begin text to compress:>"
    user_prompt = f"Create a succinct, compressed version of the following text such that you will be able to reconstruct it verbatim. You can use human legible text, or unicode / non human legible text. Use only {msg_context_length} tokens. Reply in only a few words."
    system_prompt = ""
    wandb.config.update({"user_prompt": user_prompt, "system_prompt": system_prompt})
    if list_of_experiments == "all" or "openai" in list_of_experiments:
        experiments.append(
            ExperimentConfig(
                lambda x: create_openai_helpful_message(
                    x,
                    causal_lm_tokenizer,
                    user_prompt=user_prompt,
                    system_prompt=system_prompt,
                    msg_context_length=msg_context_length,
                ),
                seq_len,
                "openai_helpful_message",
            )
        )
        print("Added openai experiment")
    if list_of_experiments == "all" or "model_helpful_message" in list_of_experiments:
        experiments.append(
            ExperimentConfig(
                lambda x: create_model_helpful_message(
                    x,
                    causal_lm_tokenizer,
                    causal_lm,
                    user_prompt=user_prompt,
                    msg_context_length=msg_context_length,
                ),
                seq_len,
                "model_helpful_message",
            )
        )
    experiments.append(
        ExperimentConfig(
            lambda x: torch.zeros((x.shape[0], 0), dtype=x.dtype, device=x.device),
            seq_len,
            "original",
        )
    )
    if list_of_experiments == "all" or "helpful_1" in list_of_experiments:
        experiments.append(
            ExperimentConfig(
                lambda x: create_helpful_message_1(x, msg_context_length),
                seq_len,
                "helpful_message_1",
            )
        )
        print("Added helpful message 1 experiment")

    losses_dict = {}
    correct_probs_all_dict = {}
    for experiment in tqdm(experiments, desc="Experiment"):
        losses, correct_probs_all = run_experiment(
            experiment,
            dataset_1_loader,
            causal_lm,
            loss_fn,
            device,
            batched_openai=batched_openai,
            verbose=verbose,
        )
        losses_dict[experiment.name] = losses
        correct_probs_all_dict[experiment.name] = correct_probs_all.clone().numpy() / (
            len(dataset_1_loader) * BATCH_SIZE
        )

    losses_mean = {}
    for exp_name, losses in losses_dict.items():
        print(f"experiment {exp_name} had avg loss of {np.mean(losses)}")
        losses_mean[exp_name] = np.mean(losses)

    # Convert LOGGING_DICT_WANDB to a DataFrame reference
    logging_df = pd.DataFrame(LOGGING_DICT_WANDB)
    # Log the DataFrame to wandb as a table
    wandb.log(
        {
            "Mean Losses": wandb.Table(
                columns=list(losses_mean.keys()), data=[list(losses_mean.values())]
            )
        }
    )
    wandb.log({"LOGGING_DICT_WANDB": wandb.Table(dataframe=logging_df)})

    data_file_name = data_file_path.split(os.path.sep)[-1]
    save_dir = os.path.join(save_dir, f"{model_name}", data_file_name)
    if not os.path.exists(f"{save_dir}"):
        os.makedirs(f"{save_dir}")
    for key in losses_dict:
        losses_dict[key] = [loss.item() for loss in losses_dict[key]]

    wandb.log({"losses_dict": losses_dict})
    df = pd.DataFrame(losses_dict)
    df["batch_index"] = df.index
    df = df.melt(id_vars=["batch_index"], value_vars=list(losses_dict.keys()))
    fig = px.line(df, x="batch_index", y="value", color="variable")
    fig.update_layout(title=f"Losses, batch_size {BATCH_SIZE}")
    fig.show()
    fig.write_html(f"{save_dir}/losses.html")
    wandb.log({"losses_html": wandb.Plotly(fig)})

    # plot the per token posisions on the same graph
    # normalize the lengths of the different experiments by padding to the max one with zeros
    wandb.log({"correct_probs_all_dict": correct_probs_all_dict})
    max_len = max([len(x) for x in correct_probs_all_dict.values()])
    for exp_name, correct_probs_all in correct_probs_all_dict.items():
        if len(correct_probs_all) < max_len:
            correct_probs_all_dict[exp_name] = np.pad(
                correct_probs_all,
                (0, max_len - len(correct_probs_all)),
                "constant",
                constant_values=0,
            )
    df = pd.DataFrame(correct_probs_all_dict)
    df["position"] = df.index
    df = df.melt(id_vars=["position"], value_vars=list(correct_probs_all_dict.keys()))
    fig = px.line(df, x="position", y="value", color="variable")
    fig.update_layout(title="Probability of correct token at each position")
    fig.show()
    fig.write_html(f"{save_dir}/probability_of_correct_token_at_each_position.html")
    wandb.log({"probability_of_correct_token_at_each_position_html": wandb.Plotly(fig)})

    wandb.config.update({"run_finished_succesfully": True}, allow_val_change=True)
    wandb.finish()


if __name__ == "__main__":
    import fire

    fire.Fire(main)
