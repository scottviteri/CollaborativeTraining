import torch
import torch.distributed as dist
import json
import matplotlib.pyplot as plt
import copy
import numpy as np
import tqdm
import random

from utilities import extend_initial_config, predict_observation
from config_examples import configs
from training_types import PerturbationConfig


def perturb_action(action, cfg):
    action_out = copy.deepcopy(action).to(cfg.device)
    offset = cfg.prefix_tensors.action_prefix_tensor.shape[-1]
    # PERTURBATION 1
    # Given n <= cfg.pure_ctxt_sizes.action_size, change token through randomization
    frac_randomize = cfg.perturbation_cfg.frac_of_tokens_to_randomize
    assert 1.0 >= frac_randomize >= 0.0, f"frac_randomize is {frac_randomize}"
    perturb_target_inds = torch.randint(
        low=offset,
        high=action.shape[-1],
        size=[int(frac_randomize * (action.shape[-1] - offset))],
    )
    action_out[:, perturb_target_inds] = torch.randint(
        low=0,
        high=cfg.tokenizer.vocab_size,
        size=[int(frac_randomize * (action.shape[-1] - offset))],
    ).to(cfg.device)

    # PERTURBATION 2
    # Given a fraction of cfg.pure_ctxt_sizes.action_size, replace with spaces/padding
    frac_spaces = cfg.perturbation_cfg.frac_of_tokens_to_pad
    assert 1.0 >= frac_spaces >= 0.0, f"frac_randomize is {frac_spaces}"
    token_id_space = cfg.tokenizer.encode(" ")[-1]
    action_out[:, offset + int((1.0 - frac_spaces) * (action.shape[-1] - offset)) :] = (
        token_id_space
    )

    # PERTURBATION 3
    # For probability p_digit_change, flip each individual digit
    p_digit_change = cfg.perturbation_cfg.p_digit_change
    assert 1.0 >= p_digit_change >= 0.0, f"p_digit_change is {p_digit_change}"
    action_out_detok = cfg.causal_lm_tokenizer.batch_decode(action_out)
    new = []
    for act in action_out_detok:
        new_act = randomize_numbers_with_probability(act, p_digit_change)
        new.append(cfg.tokenizer.encode(new_act))

    action_out = torch.tensor(new).to(cfg.device)

    return action_out


def randomize_numbers_with_probability(input_string, probability=0.5):
    output_string = ""
    for char in input_string:
        if char.isdigit():
            if random.random() < probability:
                output_string += str(random.randint(0, 9))
            else:
                output_string += char
        else:
            output_string += char
    return output_string


class ActionEvaluator:
    """Class to evaluate training trajectories (json files)"""

    def __init__(
        self,
        f_name: str,
        configs: list,
        perturbations: dict = None,
        n_max: int = None,
        n_step: int = None,
        path_to_dir: str = "saved_weights_and_losses/",
    ):
        self._f_name = f_name
        self._configs = configs
        self._n_max = n_max
        self._n_step = n_step
        self._file_path = path_to_dir + self._f_name
        self._load_traj_data()
        if perturbations is None:
            self._set_default_perturbations()
        else:
            self._perts = perturbations

    def _load_traj_data(self):
        """Load trajectory and other data from json file."""

        with open(self._file_path, "r") as file:
            self._data = json.load(file)

    def _set_default_perturbations(self):
        """"""

        self._perts = {
            "50%Rand": PerturbationConfig(
                eval_every=10,
                frac_of_tokens_to_randomize=0.5,
                frac_of_tokens_to_pad=0.0,
            ),
            "25%Rand": PerturbationConfig(
                eval_every=10,
                frac_of_tokens_to_randomize=0.25,
                frac_of_tokens_to_pad=0.0,
            ),
            "10%Rand": PerturbationConfig(
                eval_every=10,
                frac_of_tokens_to_randomize=0.1,
                frac_of_tokens_to_pad=0.0,
            ),
            "50%Spaces": PerturbationConfig(
                eval_every=10,
                frac_of_tokens_to_randomize=0.0,
                frac_of_tokens_to_pad=0.5,
            ),
            "25%Spaces": PerturbationConfig(
                eval_every=10,
                frac_of_tokens_to_randomize=0.0,
                frac_of_tokens_to_pad=0.25,
            ),
            "10%Spaces": PerturbationConfig(
                eval_every=10,
                frac_of_tokens_to_randomize=0.0,
                frac_of_tokens_to_pad=0.1,
            ),
        }

    @staticmethod
    def calculate_loss(cfg, action, obs):
        cfg.causal_lm.eval()
        cfg.inference_lm = cfg.causal_lm
        # Probably unnecessary
        cfg.inference_lm.eval()
        obs_loss = predict_observation(cfg, action, obs, add_q_head=False).mean()
        return obs_loss

    def evaluate(self):
        """"""

        if self._n_max is None:
            self._n_max = len(self._data["trajectory"])
        if self._n_step is None:
            self._n_step = 1

        eval_results = {}
        for cfg in self._configs:
            tok = cfg.tokenizer
            mod = cfg.causal_lm
            mod.eval()

            eval_loss = {"Training": [], "Pure": []}
            for keys in self._perts:
                eval_loss[keys] = []

            for step_i, step in enumerate(tqdm.tqdm(self._data["trajectory"])):
                if step_i >= self._n_max:
                    break
                if step_i % self._n_step != 0:
                    continue

                action_str = step["action"]
                obs_str = step["obs"]
                action_tok = torch.tensor(tok.encode(action_str))
                obs_tok = torch.tensor(tok.encode(obs_str))

                # Introduce a Batch Dimension
                action_tok = action_tok.view(1, -1).to(cfg.device)
                obs_tok = obs_tok.view(1, -1).to(cfg.device)

                actions_tok_pert = {}
                for key, p in self._perts.items():
                    cfg.perturbation_cfg = p
                    action_tok_pert = perturb_action(action_tok, cfg)
                    actions_tok_pert[key] = action_tok_pert
                    # ToDo: Send to cuda?
                    # print(tok.batch_decode(action_tok_pert))
                    # print()

                # Evaluate --> STEAL CODE
                with torch.no_grad():
                    # loss_pure = calculate_loss(cfg, action_tok, obs_tok)
                    # loss_pert = calculate_loss(cfg, action_tok_pert, obs_tok)

                    # print(tok.batch_decode(action_tok))
                    eval_loss["Pure"].append(
                        [step_i, self.calculate_loss(cfg, action_tok, obs_tok).item()]
                    )

                    eval_loss["Training"].append([step_i, step["observation_loss"]])

                    for key, act in actions_tok_pert.items():
                        # print(tok.batch_decode(act))
                        eval_loss[key].append(
                            [step_i, self.calculate_loss(cfg, act, obs_tok).item()]
                        )

            eval_results[cfg.model_name] = eval_loss

        return eval_results

    @staticmethod
    def plot_results(results, model_name, train_model, file_name):
        """"""

        fig, axs = plt.subplots(1, 2, figsize=(12, 4))

        fig.suptitle(f"Eval: {model_name}, Train: {train_model}")
        x_max = 0.0
        x_min = -1.0

        for keys in results:
            x, y = np.array(results[keys]).T
            x_max = np.max([x_max, np.max(x)])
            x_min = np.min([x_min, np.min(x)])

            if "Spaces" in keys:
                axs[0].plot(x, y, ".", label=keys)
            elif "Rand" in keys:
                axs[1].plot(x, y, ".", label=keys)
            elif "Training" in keys:
                axs[0].plot(x, y, "", label=keys)
                axs[1].plot(x, y, "", label=keys)
            else:
                # This case includes the "Pure" action results
                axs[0].plot(x, y, ".", label=keys)
                axs[1].plot(x, y, ".", label=keys)

        axs[0].set_title("Replacing with Spaces")
        axs[1].set_title("Swapping with Random Token")
        for i in [0, 1]:
            axs[i].set_xlabel("Training Steps [ ]")
            axs[i].set_ylabel("Average Prediction Loss [a.u.]")
            axs[i].legend(loc="upper right")
            axs[i].set_xlim(x_min - x_max * 0.1, x_max * 1.5)

        # plt.tight_layout()
        plt.savefig(f"results/{file_name[:-5]}.png")
        plt.show()


def main():
    # file_name = "gpt2_traj_1709608868.json"
    file_name = "mistral_traj_1711522015_corrected.json"

    # Set model
    init_cfg = configs[0]
    assert init_cfg.model_name == "mistral"  # data["model"]
    # init_cfg.use_mac = False  # data["model"]
    assert init_cfg.perturbation_cfg is None

    cfg = extend_initial_config(configs[0])

    eval_class = ActionEvaluator(configs=[cfg], f_name=file_name, n_step=50)
    res = eval_class.evaluate()
    print(res)
    eval_class.plot_results(
        res[cfg.model_name],
        model_name="mistral7b",
        train_model="mistral7b",
        file_name=file_name,
    )


if __name__ == "__main__":
    main()
