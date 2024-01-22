
from dataclasses import dataclass
import torch
import torchtyping
from transformers import AutoTokenizer, AutoModelForCausalLM, PreTrainedModel, PreTrainedTokenizer
from peft import LoraConfig, get_peft_model
import wandb
from dataclasses import dataclass
from typing import Optional, Union, NamedTuple, Iterable
import einops
from datasets import load_dataset

AR = NamedTuple("AR", [("observation_size", int)])
GptEval = NamedTuple("GptEval", [("num_evals", int)])
AO = NamedTuple("AO", [("use_gumbel", bool)])
AOA = NamedTuple("AOA", [("use_gumbel", bool)])
RAOInit = NamedTuple("RAO",
    [("num_rao", int),  ("obs_between_weight_updates", int), 
    ("use_loss_difference", bool), ("use_multirao_for_action_gen", bool), 
    ("use_rewards_to_go", bool)])
RAO = NamedTuple("RAO",
    [("num_rao", int),  ("obs_between_weight_updates", int), 
    ("use_loss_difference", bool), ("use_multirao_for_action_gen", bool), ("use_rewards_to_go", bool),
    ("tok_p_loss", int), ("tok_p_pure_loss", int), ("loss_prefix_tensor", torch.Tensor), ("tok_p_doc", int), ("tok_p_rao", int)])

InitTrainingType = Union[AR, GptEval, RAOInit, AOA, AO]
TrainingType = Union[AR, GptEval, RAO, AOA, AO]

@dataclass
class InitialConfig:
    model_name: str
    lr: float
    batch_size: int
    num_batches: int
    obs_to_action_ratio: float
    interval_save_weights: int
    interval_print: int
    wandb: bool
    load_model: bool
    do_lora : bool
    training_ctxt_size: Optional[int]
    dataset_name: str
    task_name: Optional[str]
    training_type : InitTrainingType

def load_cfg_from_file(file_location : str) -> InitialConfig:
    with open(file_location) as f:
        cfg_dict = json.load(f)["parameters"]
    cfg_dict["training_type"] = eval(cfg_dict["training_type"][0])(**cfg_dict["training_type"][1])
    return InitialConfig(**cfg_dict)

@dataclass
class Config:
    model_name: str
    causal_lm: PreTrainedModel
    causal_lm_tokenizer: PreTrainedTokenizer
    lr: float
    batch_size: int
    num_batches: int
    obs_to_action_ratio: float
    interval_save_weights: int
    interval_print: int
    wandb: bool
    load_model: bool
    do_lora: bool
    training_ctxt_size: int
    device: str
    dataset_name: str
    task_name: str
    path_2_log: str
    path_2_model: str
    path_2_tokenizer: str
    tok_p_action: Optional[int]
    tok_p_obs: Optional[int]
    tok_p_pure_action : Optional[int]
    tok_p_pure_obs : Optional[int]
    action_prefix_tensor: Optional[torch.Tensor]
    obs_prefix_tensor: Optional[torch.Tensor]
    ctxt_size: int
    dataloader : Iterable[torch.Tensor]
    training_type: TrainingType

def extend_initial_config(init_cfg: InitialConfig) -> Config:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    task_name = get_task_name(init_cfg.task_name, init_cfg.dataset_name, device)
    path_2_log = f"../saved_weights_and_losses/{init_cfg.model_name}_log.txt"
    path_2_model = f"../saved_weights_and_losses/{init_cfg.model_name}_weights"
    path_2_tokenizer = f"../saved_weights_and_losses/{init_cfg.model_name}_tokenizer"
    causal_lm, causal_lm_tokenizer, ctxt_size =  get_model(
        device, init_cfg.load_model, init_cfg.model_name, 
        path_2_tokenizer, path_2_model, init_cfg.do_lora
    )

    tok_p_loss, tok_p_action, tok_p_loss = None, None, None
    if isinstance(init_cfg.training_type,  RAOInit):
        training_ctxt_size = ctxt_size if init_cfg.training_ctxt_size is None else init_cfg.training_ctxt_size 
        tok_p_loss = 12
        tok_p_action = int(
                training_ctxt_size 
            / ((init_cfg.obs_to_action_ratio + 1.0) * (init_cfg.training_type.num_rao + 1.0))
            - tok_p_loss / (init_cfg.obs_to_action_ratio + 1)
        )
        tok_p_obs = int(tok_p_action * init_cfg.obs_to_action_ratio)
        tok_p_doc = tok_p_obs * init_cfg.training_type.obs_between_weight_updates
        tok_p_rao = tok_p_loss + tok_p_action + tok_p_obs
        assert tok_p_loss < init_cfg.training_ctxt_size
        assert (init_cfg.training_type.num_rao + 1) * tok_p_rao <= init_cfg.training_ctxt_size

    elif isinstance(init_cfg.training_type,  AO) or isinstance(init_cfg.training_type,  AOA):
        training_ctxt_size = ctxt_size if init_cfg.training_ctxt_size is None else init_cfg.training_ctxt_size 
        tok_p_action = int(training_ctxt_size / (init_cfg.obs_to_action_ratio + 2))
        tok_p_obs = int(tok_p_action * init_cfg.obs_to_action_ratio)
        tok_p_loss = None

    elif isinstance(init_cfg.training_type,  AR):
        tok_p_action = None
        tok_p_obs = training_type.AR.observation_size
        tok_p_loss = None
    
    elif isinstance(init_cfg.training_type,  GptEval):
        tok_p_action, tok_p_obs, tok_p_loss = None, None, None

    else:
        assert "Invalid training type"

    run_name = create_run_name(init_cfg, tok_p_loss, tok_p_action, tok_p_obs)

    tok_per_pure, prefix_tensors = get_prefixes(
        causal_lm_tokenizer, init_cfg.batch_size, device, tok_p_obs, tok_p_action, tok_p_loss)
    tok_p_pure_loss, tok_p_pure_action, tok_p_pure_obs = tok_per_pure
    loss_prefix, act_prefix, obs_prefix = prefix_tensors

    dataloader = prepare_dataset(
            init_cfg.dataset_name,
            task_name,
            causal_lm_tokenizer,
            device,
            init_cfg.num_batches,
            init_cfg.training_type.obs_between_weight_updates if isinstance(init_cfg.training_type,  RAOInit) else 1,
            tok_p_pure_obs,
            init_cfg.batch_size,
            obs_prefix 
        )

    if isinstance(init_cfg.training_type,  RAOInit):
        training_type = RAO(init_cfg.training_type.num_rao, init_cfg.training_type.obs_between_weight_updates, 
            init_cfg.training_type.use_loss_difference, init_cfg.training_type.use_multirao_for_action_gen, init_cfg.training_type.use_rewards_to_go, 
            tok_p_loss, tok_p_pure_loss, loss_prefix, tok_p_doc, tok_p_rao)
    else:
        training_type = init_cfg.training_type

    return Config(
        model_name=init_cfg.model_name,
        lr=init_cfg.lr,
        batch_size=init_cfg.batch_size,
        num_batches=init_cfg.num_batches,
        obs_to_action_ratio=init_cfg.obs_to_action_ratio,
        interval_save_weights = init_cfg.interval_save_weights,
        interval_print = init_cfg.interval_print, 
        wandb = init_cfg.wandb,
        load_model = init_cfg.load_model,
        do_lora = init_cfg.do_lora,
        training_ctxt_size = init_cfg.training_ctxt_size,
        device = device,
        dataset_name = init_cfg.dataset_name,
        dataloader = dataloader,
        task_name = get_task_name(init_cfg.task_name, init_cfg.dataset_name, device),
        path_2_log = path_2_log,
        path_2_model = path_2_model,
        path_2_tokenizer = path_2_tokenizer,
        tok_p_action = tok_p_action,
        tok_p_obs = tok_p_obs,
        tok_p_pure_action = tok_p_pure_action,
        tok_p_pure_obs = tok_p_pure_obs,
        action_prefix_tensor=act_prefix,
        obs_prefix_tensor=obs_prefix,
        ctxt_size = ctxt_size,
        causal_lm = causal_lm,
        causal_lm_tokenizer = causal_lm_tokenizer,
        training_type = training_type 
    )

def create_run_name(init_config : InitialConfig, 
    tok_p_loss : Optional[int], tok_p_action : Optional[int], tok_p_obs : Optional[int]) -> str:
    run_name = ""
    run_name += f"{init_config.model_name[:4]}_"
    if init_config.lr != 1e-4: run_name += f"lr{init_config.lr}_"

    if isinstance(init_config.training_type, AR): 
        run_name += f"AR_obs{tok_p_obs}_"

    elif isinstance(init_config.training_type, RAO): 
        run_name += f"RAO_"
        run_name += f"nr{init_config.RAO.num_rao}_"
        run_name += f"rao{tok_p_loss}/{tok_p_action}/{tok_p_obs}_"
        run_name += f"obwu{init_config.obs_between_weight_updates}_"
        if init_config.do_lora: run_name += "lora_"
        if init_config.training_type.use_loss_difference: run_name += "ld_"
        if init_config.use_multirao_for_action_gen:
            run_name += f"mr{init_config.use_multirao_for_action_gen}_"
        if init_config.use_rewards_to_go: run_name += "rtg_"

    elif isinstance(init_config.training_type, GptEval): 
        run_name += f"GptEval{init_config.training_type.observation_size}_"

    elif isinstance(init_config.training_type, AO):
        run_name += f"AO_"
        run_name += f"ao{tok_p_loss}/{tok_p_action}/{tok_p_obs}_"

    elif isinstance(init_config.training_type, AOA):
        run_name += f"AOA_"
        run_name += f"ao{tok_p_action}/{tok_p_obs}_"

    else: 
        assert f"Wrong training type: {init_config.training_type}"

    if init_config.batch_size != 1: run_name += f"bs{init_config.batch_size}_"
    run_name += f"nb{init_config.num_batches}_"
    if init_config.obs_to_action_ratio != 1:
        run_name += f"o:a={init_config.obs_to_action_ratio}:1_"
    if init_config.load_model: run_name += f"load_"
    if init_config.training_ctxt_size: run_name += f"ics{init_config.training_ctxt_size}_"
    return run_name 


def multi_print(string, f):
    print(string); print(string, file=f)

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
):
    if batch_index % cfg.interval_print == 0:
        with open(cfg.path_2_log, "a") as f:
            multi_print(f"\nBatch Number {batch_index}", f)
            multi_print(f"Loss: {batch_loss[0][0]:.3f}", f)
            if aggregate_losses:
                multi_print(f"Aggregate Loss: {aggregate_losses[-1]}", f)
            multi_print(f"Previous Obs: {repr(cfg.causal_lm_tokenizer.batch_decode(prev_obs)[0])}", f)
            multi_print(f"Action: {repr(cfg.causal_lm_tokenizer.batch_decode(action)[0])}", f)
            multi_print(f"Predicted Obs: {repr(cfg.causal_lm_tokenizer.batch_decode(predicted_obs)[0])}", f)
            multi_print(f"True Obs: {repr(cfg.causal_lm_tokenizer.batch_decode(true_obs)[0])}", f)
            multi_print("___________________________________________", f)
        if cfg.wandb:
            wandb.log(
                {
                    "Batch Index": batch_index,
                    "Batch Loss": batch_loss[0],
                }
            )


def get_prefixes(
    tokenizer:PreTrainedTokenizer, batch_size:int, device:torch.device, 
    tok_p_obs: Optional[int], tok_p_action: Optional[int], tok_p_loss: Optional[int]):

    if tok_p_obs:
        observation_prefix = "\nObservation: "
        observation_prefix_tokens = tokenizer.encode(
            observation_prefix, add_special_tokens=False
        )
        observation_prefix_tensor = einops.repeat(
            torch.tensor(observation_prefix_tokens),
            "tokens -> batch tokens",
            batch=batch_size,
        ).to(device)
        tokens_per_pure_observation = tok_p_obs - len(
            observation_prefix_tokens
        )
    else:
        observation_prefix_tensor = None
        tokens_per_pure_observation = None

    if tok_p_action:
        action_prefix = "\nAction: "
        action_prefix_tokens = tokenizer.encode(
            action_prefix, add_special_tokens=False
        )
        action_prefix_tensor = einops.repeat(
            torch.tensor(action_prefix_tokens),
            "tokens -> batch tokens",
            batch=batch_size,
        ).to(device)
        tokens_per_pure_action = tok_p_action - len(action_prefix_tokens)
    else:
        action_prefix_tensor = None
        tokens_per_pure_action = None

    if tok_p_loss:
        reward_prefix = "\nLoss: "
        reward_prefix_tokens = tokenizer.encode(
            reward_prefix, add_special_tokens=False
        )
        reward_prefix_tensor = einops.repeat(
            torch.tensor(reward_prefix_tokens),
            "tokens -> batch tokens",
            batch=batch_size,
        ).to(device)
        tokens_per_pure_reward = tok_p_loss - len(reward_prefix_tokens)
    else:
        reward_prefix_tensor = None
        tokens_per_pure_reward = None

    return ((tokens_per_pure_reward, tokens_per_pure_action, tokens_per_pure_observation), 
    (reward_prefix_tensor, action_prefix_tensor, observation_prefix_tensor))

def get_model(device, load_model, model_name, path_2_tokenizer, path_2_model, do_lora=None):
     """Load model"""
     model_dict = {
         "tinystories": "roneneldan/TinyStories-1M",
         "llama": "meta-llama/Llama-2-7b-hf",
         "distilgpt2": "distilgpt2",
         "gptj": "EleutherAI/gpt-j-6b",
         "mistral": "mistralai/Mistral-7B-v0.1",
         "gpt2-large": "gpt2-large",
         "gpt2-xl": "gpt2-xl",
         "gpt2": "gpt2",
         # Add other models here
     }

     with device:
         if load_model:
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 path_2_model,
                 torch_dtype=torch.float16,
                 use_flash_attention_2=model_name == "mistral"
                 or model_name == "llama",
             )
             causal_lm.bfloat16()
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 path_2_tokenizer,
                 padding_side="left",
             )
             if model_name == "mistral":
                 ctxt_size = causal_lm.config.sliding_window
             elif model_name == "tinystories":
                 ctxt_size = causal_lm.config.window_size
             elif model_name == "llama":
                 ctxt_size = causal_lm.config.max_position_embeddings
             elif model_name == "distilgpt2":
                 ctxt_size = causal_lm.config.n_ctx
             elif model_name == "gptj":
                 ctxt_size = causal_lm.config.n_positions
             elif model_name == "gpt2-large":
                 ctxt_size = causal_lm.config.n_positions
             else:
                 ctxt_size = causal_lm.config.n_positions

         elif model_name == "tinystories":
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 model_dict[model_name], padding_size="left"
             )
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 model_dict[model_name]
             )
             ctxt_size = causal_lm.config.window_size
         elif model_name == "llama":
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 model_dict[model_name],
                 torch_dtype=torch.float16,
                 use_flash_attention_2=True,
             )
             causal_lm.bfloat16()
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 model_dict[model_name], padding_side="left"
             )
             ctxt_size = causal_lm.config.max_position_embeddings
         elif model_name == "mistral":
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 model_dict[model_name],
                 torch_dtype=torch.float16,
                 use_flash_attention_2=True,
             )
             causal_lm.bfloat16()
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 model_dict[model_name], padding_side="left"
             )
             ctxt_size = causal_lm.config.max_position_embeddings

         elif model_name == "distilgpt2":
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 model_dict[model_name]
             )
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 model_dict[model_name], padding_side="left"
             )
             ctxt_size = causal_lm.config.n_ctx

         elif model_name == "gptj":
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 model_dict[model_name]
             )
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 model_dict[model_name], padding_side="left"
             )
             ctxt_size = causal_lm.config.n_positions

         elif model_name == "gpt2-large":
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 model_dict[model_name]
             )
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 model_dict[model_name], padding_side="left"
             )
             ctxt_size = causal_lm.config.n_ctx

         elif model_name == "gpt2-xl":
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 model_dict[model_name]
             )
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 model_dict[model_name], padding_side="left"
             )
             ctxt_size = causal_lm.config.n_ctx

         elif model_name == "gpt2":
             causal_lm = AutoModelForCausalLM.from_pretrained(
                 model_dict[model_name]
             )
             causal_lm_tokenizer = AutoTokenizer.from_pretrained(
                 model_dict[model_name], padding_side="left"
             )
             ctxt_size = causal_lm.config.n_ctx

     if do_lora:
         peft_config = LoraConfig(
             # basemodel_name_or_path=MODEL,
             r=64,
             lora_alpha=128,
             lora_dropout=0.1,
             target_modules=get_linear_layers(causal_lm),
         )

         causal_lm = get_peft_model(causal_lm, peft_config)

     causal_lm_tokenizer.padding_side = "left"
     causal_lm_tokenizer.pad_token_id = causal_lm_tokenizer.encode(" ")[0]
     return causal_lm, causal_lm_tokenizer, ctxt_size


def get_task_name(task_name, dataset_name, device):
    if task_name:
        return task_name
    if dataset_name == "wikipedia":
        if torch.cuda.is_available():
            gpu_memory = torch.cuda.get_device_properties(
                device
            ).total_memory / (1023**3)
            if gpu_memory > 49:
                task_name = "20220301.en"
            else:
                task_name = "20220301.simple"
        else:
            task_name = "20220301.simple"
    else:  # cfg.dataset_name == "bigbench":
        task_name = "arithmetic"
    return task_name

def get_linear_layers(model):
    return list(
        set(
            map(
                lambda x: x[0].split(".")[-1],
                filter(
                    lambda x: isinstance(x[1], torch.nn.Linear),
                    model.named_modules(),
                ),
            )
        )
    )


def prepare_dataset(dataset_name, task_name, tokenizer, device, num_batches, obs_between_weight_updates, tokens_per_pure_observation, batch_size, observation_prefix_tensor):
    itr_ds = load_dataset(dataset_name, task_name, split="train", streaming=True)
    ds_tokenized = map(
        lambda x: tokenizer(x["text"], return_tensors="pt")["input_ids"].to(device), 
        itr_ds)
    pure_obs = get_pure_obs(batch_size, tokens_per_pure_observation, device, ds_tokenized)
    obs_ds = prepend_obs_tensor(observation_prefix_tensor, pure_obs)
    return take(num_batches, group_obs_tensors(obs_between_weight_updates, obs_ds))    

def get_pure_obs(batch_size, tok_per_pure_obs, device, itr_ds):
    batches = [torch.empty((1,0), dtype=torch.int64, device=device) 
        for _ in range(batch_size)]
    while 1:
        for i in range(len(batches)):
            while batches[i].shape[1] < tok_per_pure_obs:
                batches[i] = torch.cat((batches[i], next(itr_ds)),dim=1)
        for batch in batches: assert  batch.shape[-1] >= tok_per_pure_obs
        out_tensor = torch.cat([batch[:,:tok_per_pure_obs] for batch in batches], dim=0)
        for i in range(len(batches)):
            batches[i] = batches[i][:, tok_per_pure_obs:]
        yield out_tensor
    return batches 

def prepend_obs_tensor(obs_prefix_tensor, itr_ds):
    return map(lambda x: torch.cat((obs_prefix_tensor, x), dim=1), itr_ds)

def group_obs_tensors(obs_per_weight_update, itr_ds):
    while 1:
        grouped = torch.stack([next(itr_ds) for _ in range(obs_per_weight_update)])
        yield einops.rearrange(grouped, 'buffer batch tokens -> batch buffer tokens')

def take(num_batches, itr_ds): 
    for _ in range(num_batches): yield next(itr_ds)