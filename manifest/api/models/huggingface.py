"""Huggingface model."""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, cast

import numpy as np
import PIL
import torch
from accelerate import dispatch_model, infer_auto_device_map
from accelerate.utils.modeling import get_max_memory as acc_get_max_memory
from transformers import (
    AutoModelForCausalLM,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    BloomForCausalLM,
    CLIPModel,
    CLIPProcessor,
    GPT2LMHeadModel,
    GPTJForCausalLM,
    GPTNeoForCausalLM,
    GPTNeoXForCausalLM,
    OPTForCausalLM,
    PreTrainedModel,
    PreTrainedTokenizer,
)

import deepspeed
from manifest.api.models.model import Model

MODEL_REGISTRY = {
    "EleutherAI/gpt-neo-125M": GPTNeoForCausalLM,
    "EleutherAI/gpt-neo-1.3B": GPTNeoForCausalLM,
    "EleutherAI/gpt-neo-2.7B": GPTNeoForCausalLM,
    "EleutherAI/gpt-j-6B": GPTJForCausalLM,
    "EleutherAI/gpt-neox-20b": GPTNeoXForCausalLM,
    "facebook/opt-125m": OPTForCausalLM,
    "facebook/opt-350m": OPTForCausalLM,
    "Salesforce/codegen-2B-mono": AutoModelForCausalLM,
    "Salesforce/codegen-6B-mono": AutoModelForCausalLM,
    "facebook/opt-1.3b": OPTForCausalLM,
    "facebook/opt-2.7b": OPTForCausalLM,
    "facebook/opt-6.7b": OPTForCausalLM,
    "facebook/opt-13b": OPTForCausalLM,
    "facebook/opt-30b": OPTForCausalLM,
    "gpt2": GPT2LMHeadModel,
    "openai/clip-vit-base-patch32": CLIPModel,
    "bigscience/bloom-560m": BloomForCausalLM,
    "bigscience/bloom-1b7": BloomForCausalLM,
    "bigscience/bloom-3b": BloomForCausalLM,
    "bigscience/bloom-7b1": BloomForCausalLM,
    "bigscience/bloom": AutoModelForCausalLM,
    "bigscience/T0pp": AutoModelForSeq2SeqLM,
    "bigscience/T0_3B": AutoModelForSeq2SeqLM,
    "google/t5-small-lm-adapt": AutoModelForSeq2SeqLM,  # 220M
    "google/t5-l-lm-adapt": AutoModelForSeq2SeqLM,  # 800M
    "google/t5-xl-lm-adapt": AutoModelForSeq2SeqLM,  # 3B
    "google/t5-xxl-lm-adapt": AutoModelForSeq2SeqLM,  # 11B
    "google/t5-v1_1-l": AutoModelForSeq2SeqLM,  # 800M
    "google/t5-v1_1-xl": AutoModelForSeq2SeqLM,  # 3B
    "google/t5-v1_1-xxl": AutoModelForSeq2SeqLM,  # 11B
    "google/flan-t5-l": AutoModelForSeq2SeqLM,  # 800M
    "google/flan-t5-xl": AutoModelForSeq2SeqLM,  # 3B
    "google/flan-t5-xxl": AutoModelForSeq2SeqLM,  # 11B
    "allenai/tk-instruct-3b-def": AutoModelForSeq2SeqLM,
}


def get_max_memory(gpu_reduction: float) -> Dict[int, str]:
    """Get max memory in GB times reduction."""
    free_in_gb = int(torch.cuda.mem_get_info()[0] / 1024**3)  # type: ignore
    max_mem = f"{int(gpu_reduction*free_in_gb)}GB"

    n_gpus = torch.cuda.device_count()
    max_mem_dict = {i: max_mem for i in range(n_gpus)}
    return max_mem_dict


class GenerationPipeline:
    """
    Custom Pipeline.

    HF pipelines do not handle devices well in multi-gpu setting.
    Create our own generation pipeline.
    """

    def __init__(
        self,
        model: Union[PreTrainedModel, deepspeed.InferenceEngine],
        tokenizer: PreTrainedTokenizer,
        device: int = None,
        bitsandbytes: bool = False,
        is_encdec: bool = False,
    ):
        """Initialize."""
        # Use to turn off sampling
        # https://github.com/TimDettmers/bitsandbytes/issues/42
        self.bitsandbytes = bitsandbytes
        self.model = model
        self.is_encdec = is_encdec
        config = model.config  # type: ignore
        # Used for GPT
        self.max_length = getattr(config, "max_position_embeddings", None)
        if self.max_length is None:
            # Used for Bloom
            self.max_length = getattr(config, "seq_length", None)
            if self.max_length is None:
                # Used for T0
                self.max_length = getattr(config, "d_model", None)
                if self.max_length is None:
                    # Default
                    self.max_length = 2048

        print(f"Usings max_length: {self.max_length}")

        self.tokenizer = tokenizer
        # self.device = device
        # With bits and bytes, do not want to place inputs on any device
        # if self.device:
        self.device = (
            torch.device("cpu")
            if (device == -1 or not torch.cuda.is_available())
            else torch.device(f"cuda:{device}")
        )

    def __call__(
        self, text: Union[str, List[str]], **kwargs: Any
    ) -> List[Dict[str, Union[str, List[float]]]]:
        """Generate from text.

        Args:
            text: text to generate.

        Returns:
            generated text.
        """
        # If text is longer than max model length, we reduce max input length to ensure
        # the user indicated generation tokens is preserved.
        max_input_len = (
            self.max_length - kwargs.get("max_new_tokens")
            if not self.is_encdec
            else self.max_length
        )
        encoded_prompt = self.tokenizer(
            text,
            max_length=max_input_len,
            truncation=True,
            padding=True,
            return_tensors="pt",
        )
        encoded_prompt = encoded_prompt.to(self.device)
        output_dict = self.model.generate(  # type: ignore
            **encoded_prompt,
            max_new_tokens=kwargs.get("max_new_tokens"),
            temperature=kwargs.get("temperature", None),
            top_k=kwargs.get("top_k", None),
            top_p=kwargs.get("top_p", None),
            repetition_penalty=kwargs.get("repetition_penalty", None),
            do_sample=kwargs.get("do_sample", None) if not self.bitsandbytes else False,
            eos_token_id=self.tokenizer.eos_token_id,
            pad_token_id=self.tokenizer.pad_token_id,
            num_return_sequences=kwargs.get("num_return_sequences", None),
            output_scores=True,
            return_dict_in_generate=True,
        )
        # logits/scores from the output always correspond to the generated tokens.
        # shape (num_tokens, num_return_sequences, vocab_size)
        logits = torch.stack(output_dict.scores)
        logits = torch.nn.functional.log_softmax(logits, dim=-1)
        num_generated_tokens = logits.shape[0]
        generated_sequences = [
            {
                "generated_text": self.tokenizer.decode(
                    output_seq[-num_generated_tokens:], skip_special_tokens=True
                ),
                "logprobs": logits[
                    range(num_generated_tokens), i, output_seq[-num_generated_tokens:]
                ].tolist(),
            }
            for i, output_seq in enumerate(output_dict.sequences)
        ]
        return generated_sequences


class HuggingFaceModel(Model):
    """HuggingFace Model."""

    def __init__(
        self,
        model_name_or_path: str,
        model_config: Optional[str] = None,
        cache_dir: Optional[str] = None,
        device: int = 0,
        use_accelerate: bool = False,
        use_parallelize: bool = False,
        use_bitsandbytes: bool = False,
        use_deepspeed: bool = False,
        perc_max_gpu_mem_red: float = 1.0,
        use_fp16: bool = False,
    ):
        """
        Initialize model.

        All arguments will be passed in the request from Manifest.

        Args:
            model_name_or_path: model name string.
            model_config: model config string.
            cache_dir: cache directory for model.
            device: device to use for model.
            use_accelerate: whether to use accelerate for multi-gpu inference.
            use_parallelize: use HF default parallelize
            use_bitsandbytes: use HF bits and bytes
            use_deepspeed: use deepspeed
            perc_max_gpu_mem_red: percent max memory reduction in accelerate
            use_fp16: use fp16 for model weights.
        """
        if sum([use_accelerate, use_parallelize, use_bitsandbytes, use_deepspeed]) > 1:
            raise ValueError(
                "Only one of use_accelerate, use_parallelize, "
                "use_bitsandbytes, use_deepspeed can be set to True"
            )
        # Check if providing path
        self.model_path = model_name_or_path
        if Path(self.model_path).exists() and Path(self.model_path).is_dir():
            # Try to find config
            if (Path(self.model_path) / "config.json").exists():
                config = json.load(open(Path(self.model_path) / "config.json"))
                model_name_or_path = config["_name_or_path"]
        self.model_name = model_name_or_path
        print("Model Name:", self.model_name, "Model Path:", self.model_path)

    def get_init_params(self) -> Dict:
        """Return init params to determine what model is being used."""
        return {"model_name": self.model_name, "model_path": self.model_path}

    def _dispatch_deepspeed_model(
        self, model: PreTrainedModel
    ) -> deepspeed.InferenceEngine:
        """
        Load model with deepspeed.

        Adapted from https://www.deepspeed.ai/tutorials/inference-tutorial/

        Args:
            model: loaded hugging face model
        """
        model = deepspeed.init_inference(
            model=model,
            mp_size=1,
            dtype=model.dtype,
            replace_method="auto",
            replace_with_kernel_inject=True,
        )
        return model

    def _dispatch_accelerate_model(
        self, model: PreTrainedModel, perc_max_gpu_mem_red: float
    ) -> None:
        """
        Load model with accelerate.

        Adapted from https://colab.research.google.com/drive/14wnxMvD9zsiBQo2FtT
                     pxn6w2cpXCcb-7#scrollTo=y8Ne7jJdaF9F&uniqifier=1

        Args:
            model: loaded hugging face model
            perc_max_gpu_mem_red: percent memory reduction
        """
        model.tie_weights()  # type: ignore
        # Get the model where we can infer devices from
        if hasattr(model, "model"):
            # OPT
            main_model = model.model  # type: ignore
            model_getter = "model."
        else:
            # Eleuther Neo and J
            main_model = model
            model_getter = ""
        # Decrease max mem
        max_memory = {
            k: int(perc_max_gpu_mem_red * v) for k, v in acc_get_max_memory().items()
        }
        raw_device_map = infer_auto_device_map(
            main_model,
            max_memory=max_memory,
            no_split_module_classes=[
                "OPTDecoderLayer",
                "GPTNeoBlock",
                "GPTJBlock",
                "GPTNeoXLayer",
                "T5Block",
            ],
            dtype=model.dtype,  # type: ignore
        )
        # Hacky fix for Eleuther getting the "weight" of embeddings
        device_map = {}
        for k, v in raw_device_map.items():
            if k in {"wte", "wpe"}:
                device_map[f"{model_getter}{k}.weight"] = v
            else:
                device_map[f"{model_getter}{k}"] = v
        # For OPT models
        if "lm_head" not in device_map:
            try:
                device_map["lm_head"] = max(device_map.values())
            except TypeError:
                device_map["lm_head"] = "cpu"
        print("Device Map", device_map)
        dispatch_model(model, device_map=device_map)
        return


class CrossModalEncoderModel(HuggingFaceModel):
    """CrossModalEncoderModel."""

    def __init__(
        self,
        model_name_or_path: str,
        model_config: Optional[str] = None,
        cache_dir: Optional[str] = None,
        device: int = 0,
        use_accelerate: bool = False,
        use_parallelize: bool = False,
        use_bitsandbytes: bool = False,
        use_deepspeed: bool = False,
        perc_max_gpu_mem_red: float = 1.0,
        use_fp16: bool = False,
    ):
        """
        Initialize model.

        All arguments will be passed in the request from Manifest.

        Args:
            model_name_or_path: model name string.
            model_config: model config string.
            cache_dir: cache directory for model.
            device: device to use for model.
            use_accelerate: whether to use accelerate for multi-gpu inference.
            use_parallelize: use HF default parallelize
            use_bitsandbytes: use HF bits and bytes
            use_deepspeed: use deepspeed
            perc_max_gpu_mem_red: percent max memory reduction in accelerate
            use_fp16: use fp16 for model weights.
        """
        super().__init__(
            model_name_or_path,
            model_config,
            cache_dir,
            device,
            use_accelerate,
            use_parallelize,
            use_bitsandbytes,
            use_deepspeed,
            perc_max_gpu_mem_red,
            use_fp16,
        )

        # TODO: make this generalizable
        self.processor = CLIPProcessor.from_pretrained(self.model_path)

        model = MODEL_REGISTRY[self.model_name].from_pretrained(
            self.model_path,
            cache_dir=cache_dir,
        )
        model.eval()

        torch_device = (
            torch.device("cpu")
            if (device == -1 or not torch.cuda.is_available())
            else torch.device(f"cuda:{device}")
        )
        print("T", torch_device)
        self.model = model.to(torch_device)  # type: ignore

    @torch.no_grad()
    def embed(self, prompt: Union[str, List[str]], **kwargs: Any) -> np.ndarray:
        """
        Compute embedding for prompts.

        Args:
            prompt: promt to generate from.

        Returns:
            embedding
        """
        if isinstance(prompt, str):
            inputs = self.processor(text=prompt, return_tensors="pt", padding=True)
        elif isinstance(prompt, PIL.Image.Image):
            inputs = self.processor(images=prompt, return_tensors="pt", padding=True)
        else:
            raise ValueError("Prompt must be a string or an image")

        outputs = self.model(**inputs)
        return outputs


class TextGenerationModel(HuggingFaceModel):
    """Huggingface model."""

    def __init__(
        self,
        model_name_or_path: str,
        model_config: Optional[str] = None,
        cache_dir: Optional[str] = None,
        device: int = 0,
        use_accelerate: bool = False,
        use_parallelize: bool = False,
        use_bitsandbytes: bool = False,
        use_deepspeed: bool = False,
        perc_max_gpu_mem_red: float = 1.0,
        use_fp16: bool = False,
    ):
        """
        Initialize model.

        All arguments will be passed in the request from Manifest.

        Args:
            model_name_or_path: model name string.
            model_config: model config string.
            cache_dir: cache directory for model.
            device: device to use for model.
            use_accelerate: whether to use accelerate for multi-gpu inference.
            use_parallelize: use HF default parallelize
            use_bitsandbytes: use HF bits and bytes
            use_deepspeed: use deepspeed
            perc_max_gpu_mem_red: percent max memory reduction in accelerate
            use_fp16: use fp16 for model weights.
        """
        super().__init__(
            model_name_or_path,
            model_config,
            cache_dir,
            device,
            use_accelerate,
            use_parallelize,
            use_bitsandbytes,
            use_deepspeed,
            perc_max_gpu_mem_red,
            use_fp16,
        )
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                self.model_name, truncation_side="left", padding_side="left"
            )
        except ValueError:
            tokenizer = AutoTokenizer.from_pretrained(
                self.model_name,
                truncation_side="left",
                padding_side="left",
                use_fast=False,
            )
        dtype = torch.float16 if use_fp16 else "auto"
        if use_bitsandbytes:
            print("WARNING!!! Cannot use sampling with bitsandbytes.")
            max_memory = get_max_memory(perc_max_gpu_mem_red)
            model = MODEL_REGISTRY[self.model_name].from_pretrained(  # type: ignore
                self.model_path,
                cache_dir=cache_dir,
                load_in_8bit=True,
                device_map="auto",
                max_memory=max_memory,
            )
        else:
            try:
                # Try to explicitely find a fp16 copy (gpt-j-6B for example)
                model = MODEL_REGISTRY[self.model_name].from_pretrained(  # type: ignore
                    self.model_path,
                    cache_dir=cache_dir,
                    revision="float16",
                    torch_dtype=torch.float16,
                )
            except Exception:
                model = MODEL_REGISTRY[self.model_name].from_pretrained(  # type: ignore
                    self.model_path, cache_dir=cache_dir, torch_dtype=dtype
                )
        model.eval()
        print(f"Loaded Model DType {model.dtype}")

        self.is_encdec = model.config.is_encoder_decoder
        if not self.is_encdec:
            tokenizer.pad_token = tokenizer.eos_token

        if not use_bitsandbytes:
            if use_accelerate:
                self._dispatch_accelerate_model(model, perc_max_gpu_mem_red)
                device = 0
            elif use_parallelize:
                model.parallelize()
                device = 0
            elif use_deepspeed:
                self._dispatch_deepspeed_model(model)
                device = 0
            else:
                if device > -1:
                    torch_device = (
                        torch.device("cpu")
                        if (device == -1 or not torch.cuda.is_available())
                        else torch.device(f"cuda:{device}")
                    )
                    model = model.to(torch_device)  # type: ignore
        self.pipeline = GenerationPipeline(  # type: ignore
            model=model,
            tokenizer=tokenizer,
            device=device,
            bitsandbytes=use_bitsandbytes,
            is_encdec=self.is_encdec,
        )

    @torch.no_grad()
    def embed(self, prompt: Union[str, List[str]], **kwargs: Any) -> np.ndarray:
        """
        Compute embedding for prompts.

        Args:
            prompt: promt to generate from.

        Returns:
            embedding
        """
        pass

    @torch.no_grad()
    def generate(
        self, prompt: Union[str, List[str]], **kwargs: Any
    ) -> List[Tuple[Any, float]]:
        """
        Generate the prompt from model.

        Outputs must be generated text and score, not including prompt.

        Args:
            prompt: promt to generate from.

        Returns:
            list of generated text (list of length 1 for 1 generation).
        """
        num_return = kwargs.get("n", 1)
        if isinstance(prompt, list) and num_return > 1:
            raise ValueError("In batch generate, n must be 1.")
        result = self.pipeline(
            prompt,
            max_new_tokens=kwargs.get("max_tokens"),
            temperature=kwargs.get("temperature"),
            repetition_penalty=kwargs.get("repetition_penalty"),
            top_k=kwargs.get("top_k"),
            top_p=kwargs.get("top_p"),
            do_sample=kwargs.get("do_sample"),
            num_return_sequences=num_return,
        )
        final_results = [
            (cast(str, r["generated_text"]), sum(cast(List[float], r["logprobs"])))
            for r in result
        ]
        return final_results

    @torch.no_grad()
    def logits_scoring(
        self, prompt: Union[str, List[str]], gold_choices: List[str], **kwargs: Any
    ) -> List[Tuple[Any, float]]:
        """
        Given the prompt and gold choices, choose the best choice with max logits.

        Args:
            prompt: promt to generate from.
            gold_choices: list of choices to choose from.

        Returns:
            the returned gold choice
        """
        if isinstance(prompt, str):
            prompt = [prompt]
        max_input_len = self.pipeline.max_length
        if self.is_encdec:
            # Adapted from https://github.com/bigscience-workshop/t-zero
            tokenized_inputs = self.pipeline.tokenizer(
                prompt,
                padding="longest",
                max_length=max_input_len,
                truncation=True,
                add_special_tokens=False,
            )
            # Get max target length
            max_target_len = max(
                [
                    len(self.pipeline.tokenizer(ans_choi)["input_ids"])
                    for ans_choi in gold_choices
                ]
            )
            tokenized_targets = [
                self.pipeline.tokenizer(
                    ans_choi,
                    # padding is on the right here.
                    padding="max_length",
                    max_length=min(max_target_len, max_input_len),
                    truncation=True,
                )
                for ans_choi in gold_choices
            ]

            # Repeat input ids for each choice to form a batch
            features = {
                k: [tokenized_inputs[k] for _ in range(len(gold_choices))]
                for k in tokenized_inputs.keys()
            }
            # Add choice tokens + mask
            features["labels"] = [
                [tokenized_targets[k]["input_ids"]] * len(tokenized_inputs["input_ids"])
                for k in range(len(gold_choices))
            ]
            features["labels_attention_mask"] = [
                [tokenized_targets[k]["attention_mask"]]
                * len(tokenized_inputs["input_ids"])
                for k in range(len(gold_choices))
            ]
        else:
            tokenized_inputs = self.pipeline.tokenizer(
                prompt,
                max_length=max_input_len,
                truncation=True,
                padding=False,
                add_special_tokens=False,
            )
            tokenized_targets = [
                self.pipeline.tokenizer(
                    # Add starting whitespace fo gpt
                    ans_choi,
                    max_length=max_input_len,
                    truncation=True,
                    padding=False,
                    add_special_tokens=False,
                )
                for ans_choi in gold_choices
            ]
            features = {
                k: [] for k in list(tokenized_inputs.keys()) + ["labels_attention_mask"]
            }
            max_effective_input_len = 0
            for tokenized_targ in tokenized_targets:
                for k in tokenized_inputs.keys():
                    batched_features = []
                    for prompt_i in range(len(tokenized_inputs[k])):
                        # Make sure to leave room for the outputs
                        batched_features.append(
                            tokenized_inputs[k][prompt_i][
                                : min(
                                    len(tokenized_inputs[k][prompt_i]),
                                    max_input_len - len(tokenized_targ[k]),
                                )
                            ]
                            + tokenized_targ[k]
                        )
                        max_effective_input_len = max(
                            max_effective_input_len, len(batched_features[-1])
                        )
                    features[k].append(batched_features)
                # Manuall add labels_attention_mask
                batched_features = []
                for prompt_i in range(len(tokenized_inputs["input_ids"])):
                    batched_features.append(
                        [0]
                        * min(
                            len(tokenized_inputs["input_ids"][prompt_i]),
                            max_input_len - len(tokenized_targ["input_ids"]),
                        )
                        + [1] * len(tokenized_targ["input_ids"])
                    )
                features["labels_attention_mask"].append(batched_features)
            # Manually pad to max effective length
            for k in features.keys():
                for targ_i in range(len(features[k])):
                    for prompt_i in range(len(features[k][targ_i])):
                        if k == "input_ids":
                            features[k][targ_i][prompt_i] += [
                                self.pipeline.tokenizer.pad_token_id
                            ] * (
                                max_effective_input_len
                                - len(features[k][targ_i][prompt_i])
                            )
                        elif k in ["attention_mask", "labels_attention_mask"]:
                            features[k][targ_i][prompt_i] += [0] * (
                                max_effective_input_len
                                - len(features[k][targ_i][prompt_i])
                            )
                        else:
                            raise ValueError(f"Unknown key {k} for decoder only models")

            features["labels"] = features["input_ids"]

        # Convert to tensors
        tensor_features = {}
        for k in features:
            tensor_features[k] = torch.LongTensor(features[k]).to(self.pipeline.device)

        if self.is_encdec:
            gold_l, bsz, seq_len = tensor_features["labels"].shape
            stacked_logits = self.pipeline.model(  # type: ignore
                input_ids=tensor_features["input_ids"].reshape(gold_l * bsz, -1),
                attention_mask=tensor_features["attention_mask"].reshape(
                    gold_l * bsz, -1
                ),
                labels=tensor_features["labels"].reshape(gold_l * bsz, -1),
            ).logits
            stacked_logits = stacked_logits.reshape(gold_l, bsz, seq_len, -1)
            # Adapted from https://github.com/bigscience-workshop/t-zero
            masked_log_probs = tensor_features["labels_attention_mask"].unsqueeze(
                -1
            ) * torch.log_softmax(stacked_logits, dim=-1)
            seq_token_log_probs = torch.gather(
                masked_log_probs, -1, tensor_features["labels"].unsqueeze(-1)
            )
        else:
            stacked_logits = self.pipeline.model(  # type: ignore
                input_ids=tensor_features["input_ids"],
                attention_mask=tensor_features["attention_mask"],
            ).logits
            # For causal decoders, shift logts and labels
            labels_attention_mask = tensor_features["labels_attention_mask"].unsqueeze(
                -1
            )[..., 1:, :]
            masked_log_probs = (
                labels_attention_mask.float()
                * torch.log_softmax(stacked_logits.float(), dim=-1)[..., :-1, :]
            )
            seq_token_log_probs = torch.gather(
                masked_log_probs, -1, tensor_features["labels"][..., 1:].unsqueeze(-1)
            )
        seq_token_log_probs = seq_token_log_probs.squeeze(dim=-1)
        seq_log_prob = seq_token_log_probs.sum(dim=-1)
        # Averaging over output sequence length for GPT
        if not self.is_encdec:
            seq_log_prob = seq_log_prob * (1 / (seq_token_log_probs != 0).sum(dim=-1))
        prediction = seq_log_prob.argmax(dim=0)
        return [
            (gold_choices[int(p)], seq_log_prob[int(p), i].item())
            for i, p in enumerate(prediction)
        ]

    @torch.no_grad()
    def score_sequence(
        self, prompt: Union[str, List[str]], **kwargs: Any
    ) -> List[float]:
        """
        Score a sequence of choices.

        Args:
            prompt (:obj:`str` or :obj:`List[str]`):
                The prompt to score the choices against.
            **kwargs:
                Additional keyword arguments passed along to the :obj:`__call__` method.
        """
        if isinstance(prompt, str):
            prompt = [prompt]
        encoded_prompt = self.pipeline.tokenizer(
            prompt,
            max_length=self.pipeline.max_length,
            truncation=True,
            padding=True,
            return_tensors="pt",
        )
        encoded_prompt["labels"] = encoded_prompt["input_ids"].clone()
        encoded_prompt = encoded_prompt.to(self.pipeline.device)
        logits = self.pipeline.model(  # type: ignore
            **encoded_prompt,
        ).logits
        # For causal decoders, shift logts and labels
        labels_attention_mask = encoded_prompt["attention_mask"].unsqueeze(-1)[
            ..., 1:, :
        ]
        masked_log_probs = (
            labels_attention_mask.float()
            * torch.log_softmax(logits.float(), dim=-1)[..., :-1, :]
        )
        seq_token_log_probs = torch.gather(
            masked_log_probs, -1, encoded_prompt["labels"][..., 1:].unsqueeze(-1)
        )
        seq_token_log_probs = seq_token_log_probs.squeeze(dim=-1)
        seq_log_prob = seq_token_log_probs.sum(dim=-1)
        return seq_log_prob.tolist()
