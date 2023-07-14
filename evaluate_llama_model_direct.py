"""
This is a script to evaluate a llama model on some data with respect to a reward model.
"""

import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    AutoModelForSequenceClassification,
    PreTrainedTokenizerBase,
    LlamaConfig,
    LlamaForCausalLM,
    LlamaTokenizer,
)
from tqdm import tqdm
from analyze_messages import extract_dataset
import matplotlib.pyplot as plt

# Load model directly
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from typing import Any

class MockRewardModel(torch.nn.Module):
    """Mocks a HuggingFace AutoModelForSequenceClassification class."""

    def __init__(self) -> None:
        """Mocks initialization."""
        super().__init__()
        self.device = torch.device("cpu")

    def __call__(
        self,
        input_ids: torch.LongTensor,
        **_: Any,
    ) -> Any:
        """Mocks the __call__ method for sequence classification."""
        output = type("", (), {})()  # TODO use an actual mocking library
        # Return a random float for each input in the batch
        output.logits = torch.randn(input_ids.shape[0])
        return output

    def forward(
        self,
        **kwargs: Any,
    ) -> Any:
        """Mocks the forward method for sequence classification."""
        return self(**kwargs)


# This is a class that evaluates a llama model.
# It stores things like the reward model, the dataset, the language model,
# and the tokenizer.
class LlamaEvaluator:
    def __init__(self, language_model_name, reward_model_name, dataset):
        self.tokenizer, self.language_model = self.load_language_model(language_model_name)
        self.reward_model = self.load_reward_model(reward_model_name)
        self.dataset = dataset

    def load_language_model(self, language_model_name):
        """
        Loads a llama model
        """
        tokenizer = LlamaTokenizer.from_pretrained(language_model_name)
        model = LlamaForCausalLM.from_pretrained(language_model_name)
        return tokenizer, model


    def load_reward_model(self, reward_model_name):
        """
        Loads a reward model
        """
        if "mock" in reward_model_name:
            return MockRewardModel()

    def generate_one_completion(self, prompt):
        """
        Generates a completion for a prompt
        """
        self.language_model.eval()
        input_ids = self.tokenizer.encode(prompt, return_tensors="pt")
        if len(input_ids) > 200:
            input_ids = input_ids[:100]
            print("Warning: Truncating prompt to 100 tokens.")
        outputs = self.language_model.generate(
            input_ids,
            max_length=200,
            do_sample=True,
            top_k=50,
            top_p=0.95,
            num_return_sequences=1,
        )
        return [self.tokenizer.decode(output) for output in outputs]

    def evaluate(self, cap_num_prompts=-1):
        """
        Evaluates the model on the dataset
        """

        # (1) generate completions for each prompt in the dataset
        # (2) feed the completions into the reward model
        # (3) compute the average reward for each completion

        # (1)
        self.language_model.eval()
        completions = []
        if cap_num_prompts > 0:
            dataset = self.dataset[:cap_num_prompts]
            print("Warning: Capping number of prompts to {}.".format(cap_num_prompts))
        else:
            dataset = self.datasets
        for prompt in tqdm(dataset, desc="Generating completions"):
            completions.extend(self.generate_one_completion(prompt))

        # (2)
        rewards = []
        for completion in tqdm(completions, desc="Computing rewards"):
            input_ids = self.tokenizer.encode(completion, return_tensors="pt")
            rewards.append(self.reward_model(input_ids).logits.item())

        # (3)
        return rewards

tokenizer = AutoTokenizer.from_pretrained("OpenAssistant/reward-model-deberta-v3-large-v2")
reward_model = AutoModelForSequenceClassification.from_pretrained("OpenAssistant/reward-model-deberta-v3-large-v2")

def get_rewards(i):
    dataset = extract_dataset(i)
    #evaluator = LlamaEvaluator("peterchatain/mock_llama", "mockRM", dataset)
    # (2)
    rewards = []
    for completion in tqdm(dataset, desc="Computing rewards"):
        input_ids = tokenizer.encode(completion, return_tensors="pt")
        rewards.append(reward_model(input_ids).logits.item())
    return rewards


def evaluate_test():
    plt.figure()

    plt.plot(get_rewards(0), label='Agent 1')
    plt.plot(get_rewards(1), label='Agent 2')
    plt.plot(get_rewards(2), label='Agent 3')

    plt.legend() # Add legend
    plt.ylabel('Reward') # Add y label
    plt.xlabel('Episode') # Add x label
    plt.title('Reward per Episode') # Add plot title
    plt.savefig('out.png')

def main():
    evaluate_test()

if __name__ == "__main__":
    main()