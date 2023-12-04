"""Script to provide rao training and sequence generation tools.

Defines MyRAO struct-like class
Defines RaoConfig for initialization and immutable conf parameters

"""

from dataclasses import dataclass
import torch
import torchtyping
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import LoraConfig, get_peft_model
import wandb

# from collections import namedtuple
# Config = namedtuple("Config", ["setting1", "setting2"])


@dataclass
class MyRAO:
    r: torchtyping.TensorType
    a: torchtyping.TensorType
    o: torchtyping.TensorType


class RaoConfig:
    """Immutable config class to set up rao-like training and data generation."""

    def __init__(
        self,
        device=None,
        wandb: bool = False,
        load_model: bool = False,
        do_lora: bool = True,
        model_name: str = "distilgpt2",
        save_dir: str = "./",
        path_2_model: str = None,
        tok_p_reward: int = 10,
        tok_p_action: int = 100,
        tok_p_obs: int = 50,
        obs_p_doc: int = 5,
        batch_size: int = 2,
        num_batches: int = 4,
        interval_save_weights: int = 30,
        interval_print: int = None,
    ):
        if device is None:
            self._device = torch.device("cuda" if torch.cuda.is_available() else "mps")
        else:
            self._device = device

        # bools
        self._load_model = load_model
        self._wandb = wandb
        self._do_lora = do_lora

        # strs
        self._model_name = model_name
        self._save_dir = save_dir
        self._path_2_model = path_2_model

        # ints
        self._tok_p_reward = tok_p_reward
        self._tok_p_action = tok_p_action
        self._obs_p_doc = obs_p_doc
        if tok_p_obs is None:
            self._tok_p_obs = self._ctxt_size - self._tok_p_action - self._tok_p_reward
        else:
            self._tok_p_obs = tok_p_obs
        self._tok_p_rao = self._tok_p_reward + self._tok_p_action + self._tok_p_obs
        self._tok_p_doc = self._tok_p_obs * self._obs_p_doc
        self._batch_size = batch_size
        self._num_batches = num_batches
        self._interval_save_weights = interval_save_weights
        if interval_print is None:
            self._interval_print = (
                5 if self._model_name == "gptj" or self._model_name == "mistral" else 10
            )
        else:
            self._interval_print = interval_print

        # sets model, tokenizer and ctxt_size
        self._set_model()

    def __repr__(self):
        return (
            f"RaoConfig({self._model_name}, do_lora {self._do_lora}, "
            + f"batch_size {self._batch_size}, tok_p_action {self._tok_p_action}, "
            + f"ctxt_size {self._ctxt_size})"
        )

    def _set_model(self):
        """Load model"""

        if self._load_model:
            assert (
                self._path_2_model is not None
            ), f"Path to model not set {self._path_2_model}"
            causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                f"/content/drive/MyDrive/CollaborativeTrainingModelWeights/tokenizer_{self._model_name}",
                padding_size="left",
            )
            causal_lm = AutoModelForCausalLM.from_pretrained(
                f"/content/drive/MyDrive/CollaborativeTrainingModelWeights/trained_{self._model_name}"
            )
            causal_lm.to(self._device)
            if self._model_name == "gptj":
                self._ctxt_size = causal_lm.config.n_positions
            elif self._model_name == "gptj":
                self._ctxt_size = causal_lm.config.sliding_window
            else:
                self._ctxt_size = causal_lm.config.n_ctx

        elif self._model_name == "mistral":
            causal_lm = AutoModelForCausalLM.from_pretrained(
                "mistralai/Mistral-7B-v0.1",
                torch_dtype=torch.float16,
                use_flash_attention_2=True,
            ).to(self._device)
            causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                "mistralai/Mistral-7B-v0.1", padding_side="left"
            )
            self._ctxt_size = causal_lm.config.sliding_window

        elif self._model_name == "distilgpt2":
            causal_lm = AutoModelForCausalLM.from_pretrained("distilgpt2").to(
                self._device
            )
            causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                "distilgpt2", padding_side="left"
            )
            self._ctxt_size = causal_lm.config.n_ctx

        elif self._model_name == "gptj":
            causal_lm = AutoModelForCausalLM.from_pretrained("EleutherAI/gpt-j-6b").to(
                self._device
            )
            causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                "EleutherAI/gpt-j-6b", padding_side="left"
            )
            self._ctxt_size = causal_lm.config.n_positions

        elif self._model_name == "gpt2-large":
            causal_lm = AutoModelForCausalLM.from_pretrained("gpt2-large").to(
                self._device
            )
            causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                "gpt2-large", padding_side="left"
            )
            self._ctxt_size = causal_lm.config.n_ctx

        elif self._model_name == "gpt2-xl":
            causal_lm = AutoModelForCausalLM.from_pretrained("gpt2-xl").to(self._device)
            causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                "gpt2-xl", padding_side="left"
            )
            self._ctxt_size = causal_lm.config.n_ctx

        elif self._model_name == "gpt2":
            causal_lm = AutoModelForCausalLM.from_pretrained("gpt2").to(self._device)
            causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                "gpt2", padding_side="left"
            )
            self._ctxt_size = causal_lm.config.n_ctx

        if self._do_lora:
            peft_config = LoraConfig(
                # base_model_name_or_path=MODEL,
                r=64,
                lora_alpha=128,
                lora_dropout=0.1,
                target_modules=self.get_linear_layers(causal_lm),
            )

            causal_lm = get_peft_model(causal_lm, peft_config)

        causal_lm_tokenizer.pad_token_id = causal_lm_tokenizer.eos_token_id
        self._model = causal_lm
        self._tokenizer = causal_lm_tokenizer

    @property
    def device(self):
        return self._device

    @property
    def load_model(self):
        return self._load_model

    @property
    def wandb(self):
        return self._wandb

    @property
    def do_lora(self):
        return self._do_lora

    @property
    def model_name(self):
        return self._model_name

    # I do not know if immutability is an issue for a trainable model
    @property
    def model(self):
        return self._model

    @property
    def tokenizer(self):
        return self._tokenizer

    @property
    def ctxt_size(self):
        return self._ctxt_size

    @property
    def save_dir(self):
        return self._save_dir

    @property
    def tok_p_reward(self):
        return self._tok_p_reward

    @property
    def tok_p_action(self):
        return self._tok_p_action

    @property
    def tok_p_obs(self):
        return self._tok_p_obs

    @property
    def tok_p_rao(self):
        return self._tok_p_rao

    @property
    def tok_p_doc(self):
        return self._tok_p_doc

    @property
    def obs_p_doc(self):
        return self._obs_p_doc

    @property
    def batch_size(self):
        return self._batch_size

    @property
    def num_batches(self):
        return self._num_batches

    @property
    def interval_save_weights(self):
        return self._interval_save_weights

    @property
    def interval_print(self):
        return self._interval_print

    @staticmethod
    def get_linear_layers(model):
        return list(set(
            map(
                lambda x: x[0].split(".")[-1],
                filter(
                    lambda x: isinstance(x[1], torch.nn.Linear),
                    model.named_modules(),
                ),
            ))
        )


def log_and_print_info(
    cfg,
    batch_index,
    observation_index,
    batch_loss,
    aggregate_losses,
    prev_obs,
    action,
    predicted_obs,
    true_obs,
    optimizer,
    wandb_table,
):
    tokenizer = cfg.tokenizer
    if (
        batch_index % cfg.interval_print == 0
        and observation_index % (cfg.obs_p_doc//2) == 0
    ):
        print(f"\nBatch number {batch_index}")
        print("batch loss: ", batch_loss[0])
        if aggregate_losses:
            print("aggregate loss: ", aggregate_losses[-1])
        print("previous obs:", repr(tokenizer.batch_decode(prev_obs)[0]))
        print("action: ", repr(tokenizer.batch_decode(action)[0]))
        print("predicted obs: ", repr(tokenizer.batch_decode(predicted_obs)[0]))
        print("true obs:", repr(tokenizer.batch_decode(true_obs)[0]))
        for param_group in optimizer.param_groups:
            print("Current learning rate: ", param_group["lr"])
    with open(f"{cfg.save_dir}/{cfg.model_name}_training_info.txt", "a") as f:
        print(f"\nBatch number {batch_index}", file=f)
        print("batch loss: ", batch_loss[0], file=f)
        if aggregate_losses:
            print("aggregate loss: ", aggregate_losses[-1], file=f)
        print("previous obs:", repr(tokenizer.batch_decode(prev_obs)[0]), file=f)
        print("action: ", repr(tokenizer.batch_decode(action)[0]), file=f)
        print(
            "predicted obs: ",
            repr(tokenizer.batch_decode(predicted_obs)[0]),
            file=f,
        )
        print("true obs:", repr(tokenizer.batch_decode(true_obs)[0]), file=f)
        for param_group in optimizer.param_groups:
            print("Current learning rate: ", param_group["lr"], file=f)
    if cfg.wandb:
        wandb.log(
            {
                "Batch number": batch_index,
                "Batch Loss": batch_loss[0].item(),
                # "Aggregate loss": aggregate_losses[-1] if aggregate_losses else -1,
                "Current learning rate": [
                    g["lr"] for g in optimizer.param_groups if "lr" in g
                ][0],
            }
        )
        wandb_table.add_data(
            repr(tokenizer.batch_decode(prev_obs)[0]),
            repr(tokenizer.batch_decode(action)[0]),
            repr(tokenizer.batch_decode(predicted_obs)[0]),
            repr(tokenizer.batch_decode(true_obs)[0]),
        )


def main():
    # define each class in default to check if they work
    test0 = MyRAO(r=torch.ones(1), a=torch.ones(10), o=torch.ones(10))
    print(test0)
    test1 = RaoConfig()
    print(test1)


if __name__ == "__main__":
    main()
