import torch
import einops
from datasets import load_dataset
from itertools import islice, tee
from typing import Iterator, Dict

import json
import operator
import numpy as np

from src.prepare_dataset import *
from src.training_types import *

def prepare_dataset(
    init_cfg,
    causal_lm_tokenizer,
    device,
    action_prefix_tensor,
    obs_prefix_tensor,
    tok_p_pure_action,
    tok_p_pure_obs,
    action_prefix,
    obs_prefix,
):
    task = init_cfg.dataset.task

    if isinstance(task, ArithmeticTask):
        itr_ds = arithmetic_generator(
            task.num_terms,
            task.num_digits,
            task.operations,
            task.probs,
        )
        qa_traj_itr = to_qa_traj_itr(itr_ds)
        qa_batch_lst_itr = traj_itr_to_batch_lst(init_cfg.batch_size, qa_traj_itr)
        qa_tokenized_itr_ds = map(
            lambda batch_lst: [
                tokenize_and_pad(
                    device,
                    causal_lm_tokenizer,
                    action_prefix_tensor,
                    obs_prefix_tensor,
                    tok_p_pure_action,
                    tok_p_pure_obs,
                    b,
                )
                for b in batch_lst
            ],
            qa_batch_lst_itr,
        )

        # we only support synchronized batches (as in have the same Action existence ordering)
        # each batch is a list of dicts -> turn into a single dict
        # indexed by the keys of the first dictionary
        dict_ds = map(
            lambda batch_list: {
                k: (
                    torch.stack([d[k] for d in batch_list], dim=0)
                    if isinstance(batch_list[0][k], torch.Tensor)
                    else batch_list[0][k]
                )
                for k in batch_list[0]
            },
            qa_tokenized_itr_ds,
        )

    elif isinstance(task, WikipediaTask):
        itr_ds = iter(
            load_dataset("wikipedia", "20220301.en", split="train", streaming=True)
        )
        ds_tokenized = map(
            lambda x: causal_lm_tokenizer(x["text"], return_tensors="pt")[
                "input_ids"
            ].to(device),
            itr_ds,
        )
        pure_obs = get_pure_obs(
            init_cfg.batch_size, tok_p_pure_obs, device, ds_tokenized
        )
        dict_ds = map(lambda x: {"Observation": x}, pure_obs)

    else:
        assert False, "Unknown dataset"

    if isinstance(init_cfg.debug, ReplaceWithRandomTokens):
        dict_ds = map(
            lambda d: {
                **d,
                "Observation": torch.randint(
                    0,
                    causal_lm_tokenizer.vocab_size,
                    d["Observation"].shape,
                    device=d["Observation"].device,
                    dtype=d["Observation"].dtype,
                ),
            },
            dict_ds,
        )

    if init_cfg.dataset.peek_every is None:
        dict_ds = map(
            lambda d: {"Observation": d["Observation"], "First": d["First"]}, dict_ds
        )
    else:
        dict_ds = peek_every_n(init_cfg.dataset.peek_every, dict_ds)
    # dict_ds = prepend_prefix_tensors(obs_prefix, action_prefix, dict_ds)
    # Note: is it true that RAO uses the same num_batches to count the number of weight updates?
    #  this would make them not comparable to AO, unless we divide by obs per weight update
    if isinstance(init_cfg.debug, RepeatNPoints):
        dict_ds = repeat_every_n_points(init_cfg.debug.num_points, dict_ds)
    elif isinstance(init_cfg.debug, RepeatPointNTimes):  # 1 time is identity
        dict_ds = repeat_point_n_times(init_cfg.debug.num_times, dict_ds)
    dict_ds = group_pairs(dict_ds)
    return take(init_cfg.num_batches, dict_ds)

def peek_every_n(n, dict_itr):
    i = 0
    for d in dict_itr:
        if "Action" in d:
            if i % n == 0:
                yield d
            else:
                yield {"Observation": d["Observation"], "First": d["First"]}
            i += 1
        else:
            yield {"Observation": d["Observation"], "First": d["First"]}

def arithmetic_generator(num_terms, num_digits, operations, probs):
    # If not specified, use simple addition
    if operations is None:
        operations = ["+"]
    # Use uniform distribution for operators if not set
    if probs is None:
        probs = [(1.0 / len(operations)) for _ in range(len(operations))]

    # Check for valid operations
    valid_ops = {"+": operator.add, "-": operator.sub, "*": operator.mul}
    for op in operations:
        assert (
            op in valid_ops.keys()
        ), f"Invalid operation {op} not in {valid_ops.keys()}"
    assert len(probs) == len(operations), "len(Operations) != len(probs)"

    while 1:
        question = "Question: "
        total = 0.0
        nums = torch.randint(0, 10**num_digits - 1, (num_terms,))
        ops_rand = np.random.choice(operations, num_terms - 1, p=probs)

        for i in range(num_terms):
            num = nums[i]
            if i == 0:
                total = nums[0].item()
                question += f"{num} "
            else:
                op_rand = ops_rand[i - 1]
                total = valid_ops[op_rand](total, num)
                question += f"{op_rand} {num} "
        question = question[:-1] + "."

        answer = f"Answer: {total}"
        yield {"Question": question, "Answer": answer}

def jsonl_to_dict_iterator(filename: str) -> Iterator[Dict]:
    with open(filename, "r") as infile:
        for line in infile:
            yield json.loads(line)

def concat_batches_to_len(length, itr):
    batch = next(itr)
    while 1:
        while batch.shape[1] < length:
            batch = torch.cat((batch, next(itr)), dim=1)
        yield batch[:, :length]
        batch = batch[:, length:]

def batch(batch_size, itr):
    while 1:
        new_list = [next(itr) for _ in range(batch_size)]
        yield new_list

def flatten(itrs):
    while 1:
        for itr in itrs:
            yield next(itr)

def nth_iterators(n, itr):
    itrs = tee(itr, n)
    return (islice(itr, i, None, n) for i, itr in enumerate(itrs))

def group_pairs(itr):
    first = next(itr)
    while 1:
        second = next(itr)
        yield (first, second)
        first = second

def tokenize_and_pad(
    device,
    tokenizer,
    action_prefix_tensor,
    obs_prefix_tensor,
    tok_p_pure_action,
    tok_p_pure_obs,
    d,
):
    action_prefix_tensor = action_prefix_tensor.to(device)
    obs_prefix_tensor = obs_prefix_tensor.to(device)
    # indexing here because mistral tokenizer adds two tokens to the beginning!
    # but it shouldn't if add_special_tokens=False ...
    obs_tok = tokenizer(
        d["Observation"], add_special_tokens=False, return_tensors="pt"
    )["input_ids"][0].to(
        device
    )  # [2:]
    assert len(obs_tok) < tok_p_pure_obs
    obs_pad_tok = torch.full(
        (tok_p_pure_obs - len(obs_tok),),
        tokenizer.pad_token_id,
        dtype=torch.int64,
        device=device,
    )
    if "Action" in d:
        action_tok = tokenizer(
            d["Action"], add_special_tokens=False, return_tensors="pt"
        )["input_ids"][0].to(device)
        assert len(action_tok) < tok_p_pure_action
        action_pad_tok = torch.full(
            (tok_p_pure_action - len(action_tok),),
            tokenizer.pad_token_id,
            dtype=torch.int64,
            device=device,
        )
        return {
            "Observation": torch.cat([obs_prefix_tensor[0], obs_tok, obs_pad_tok]),
            "Action": torch.cat([action_prefix_tensor[0], action_tok, action_pad_tok]),
            "First": d["First"],
        }
    else:
        return {
            "Observation": torch.cat([obs_prefix_tensor[0], obs_tok, obs_pad_tok]),
            "First": d["First"],
        }

def to_qa_traj_itr(itr):
    while 1:
        d = next(itr)
        if "Explanation" in d:
            yield iter(
                [
                    {"Observation": d["Question"], "First": True},
                    {
                        "Action": d["Explanation"],
                        "Observation": d["Answer"],
                        "First": False,
                    },
                ]
            )
        else:
            yield iter(
                [
                    {"Observation": d["Question"], "First": True},
                    {"Observation": d["Answer"], "First": False},
                ]
            )

def get_pure_obs(batch_size, tok_per_pure_obs, device, itr_ds):
    # batch in a way that keeps state so that you only add to a batch index
    #  with fewer than tok_per_pure_obs tokens
    batches = [
        torch.empty((1, 0), dtype=torch.int64, device=device) for _ in range(batch_size)
    ]
    while 1:
        for i in range(len(batches)):
            while batches[i].shape[1] < tok_per_pure_obs:
                batches[i] = torch.cat((batches[i], next(itr_ds)), dim=1)
        for batch in batches:
            assert batch.shape[-1] >= tok_per_pure_obs
        out_tensor = torch.cat(
            [batch[:, :tok_per_pure_obs] for batch in batches], dim=0
        )
        for i in range(len(batches)):
            batches[i] = batches[i][:, tok_per_pure_obs:]
        yield out_tensor
    return batches

def traj_itr_to_batch_lst(batch_size, traj_itr):
    batch_itrs = [next(traj_itr) for _ in range(batch_size)]
    while 1:
        out_lst = []
        for i in range(batch_size):
            try:
                next_item = next(batch_itrs[i])
            except StopIteration:
                batch_itrs[i] = next(traj_itr)
                next_item = next(batch_itrs[i])
            out_lst.append(next_item)
        yield out_lst

def prepend_prefix_tensors(obs_prefix_tensor, action_prefix_tensor, itr_ds):
    out_d = {}
    for d in itr_ds:
        if "Observation" in d:
            out_d["Observation"] = torch.cat(
                (obs_prefix_tensor, d["Observation"]), dim=1
            )
        if "Action" in d:
            out_d["Action"] = torch.cat((action_prefix_tensor, d["Action"]), dim=1)
    return out_d

def stack_batch(batch):
    # do I want each batch to separately be able to add actions? Seems unnecessary for now
    grouped_obs = torch.stack([d["Observation"] for d in batch])
    if "Action" in batch[0]:
        grouped_actions = torch.stack([d["Action"] for d in batch])
        return {"Observation": grouped_obs, "Action": grouped_actions}
    return {"Observation": grouped_obs}

def stack_buffer(batch):
    # do I want each batch to separately be able to add actions? Seems unnecessary for now
    grouped_obs = torch.stack([d["Observation"] for d in batch])
    if "Action" in batch[0]:
        grouped_actions = torch.stack([d["Action"] for d in batch])
        return {
            "Observation": einops.rearrange(
                grouped_obs, "buffer batch tokens -> batch buffer tokens"
            ),
            "Action": einops.rearrange(
                grouped_actions, "buffer batch tokens -> batch buffer tokens"
            ),
        }
    return {
        "Observation": einops.rearrange(
            grouped_obs, "buffer batch tokens -> batch buffer tokens"
        )
    }

def take(num_batches, itr_ds):
    for _ in range(num_batches):
        yield next(itr_ds)

def repeat_every_n_points(n, itr):
    first_n_vals = [next(itr) for _ in range(n)]
    i = 0
    while 1:
        yield first_n_vals[i]
        i = (i + 1) % n

def debug(itr):
    next_val = next(itr)
    print(next_val)
    yield next_val

def debug_shape(itr):
    next_val = next(itr)
    print(next_val.shape)
    yield next_val

def repeat_point_n_times(n, itr):
    while 1:
        next_val = next(itr)
        for _ in range(n):
            yield next_val
