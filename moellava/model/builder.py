#    Copyright 2023 Haotian Liu
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.


import os
import warnings
import shutil

from moellava.model.language_model.llava_qwen_moe import EvalMoELLaVAQWenForCausalLM
from moellava.model.language_model.llava_qwen import LlavaQWenForCausalLM

from moellava.model.language_model.llava_llama_moe import EvalMoELLaVALlamaForCausalLM
from moellava.model.language_model.llava_llama import LlavaLlamaForCausalLM
from transformers import AutoTokenizer, AutoModelForCausalLM, AutoConfig, BitsAndBytesConfig, GenerationConfig
import torch
from moellava.model import *
from moellava.constants import DEFAULT_IMAGE_PATCH_TOKEN, DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN, \
    DEFAULT_VID_END_TOKEN, DEFAULT_VID_START_TOKEN, DEFAULT_VIDEO_PATCH_TOKEN
from moellava.model.language_model.qwen.tokenization_qwen import QWenTokenizer


def load_pretrained_model(model_path, model_base, model_name, load_8bit=False, load_4bit=False, device_map="auto", device="cuda", **kwargs):
    kwargs = {"device_map": device_map, **kwargs}

    if device != "cuda":
        kwargs['device_map'] = {"": device}

    if load_8bit:
        kwargs['load_in_8bit'] = True
    elif load_4bit:
        kwargs['load_in_4bit'] = True
        kwargs['quantization_config'] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type='nf4'
        )
    else:
        kwargs['torch_dtype'] = torch.float16

    if 'llava' in model_name.lower():
        # Load LLaVA model
        if 'lora' in model_name.lower() and model_base is None:
            warnings.warn('There is `lora` in model name but no `model_base` is provided. If you are loading a LoRA model, please provide the `model_base` argument. Detailed instruction: https://github.com/haotian-liu/LLaVA#launch-a-model-worker-lora-weights-unmerged.')
        if 'lora' in model_name.lower() and model_base is not None:
            lora_cfg_pretrained = AutoConfig.from_pretrained(model_path)
            tokenizer = AutoTokenizer.from_pretrained(model_base, use_fast=False)
            print('Loading LLaVA from base model...')
            # ============================================================================================= TODO: adapt to moe
            if getattr(lora_cfg_pretrained, 'moe_enable', False):
                raise NotImplementedError
                if 'qwen' in model_base.lower():
                    model = EvalMoELLaVAQWenForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=lora_cfg_pretrained, **kwargs)
                    model.generation_config = GenerationConfig.from_pretrained(model_base, pad_token_id=tokenizer.pad_token_id)
                else:
                    model = EvalMoELLaVALlamaForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=lora_cfg_pretrained, **kwargs)
                import deepspeed
                deepspeed.init_distributed(dist_backend='nccl', auto_mpi_discovery=False)
                # Initialize the DeepSpeed-Inference engine
                ds_engine = deepspeed.init_inference(model,
                                                     # mp_size=2,
                                                     # dtype=torch.half,
                                                     checkpoint=None,
                                                     replace_with_kernel_inject=True)
                model = ds_engine.module
            else:
                if 'qwen' in model_base.lower():
                    model = LlavaQWenForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=lora_cfg_pretrained, **kwargs)
                    model.generation_config = GenerationConfig.from_pretrained(model_base, pad_token_id=tokenizer.pad_token_id)
                else:
                    model = LlavaLlamaForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=lora_cfg_pretrained, **kwargs)
            # =============================================================================================
            token_num, tokem_dim = model.lm_head.out_features, model.lm_head.in_features
            if model.lm_head.weight.shape[0] != token_num:
                model.lm_head.weight = torch.nn.Parameter(torch.empty(token_num, tokem_dim, device=model.device, dtype=model.dtype))
                model.model.embed_tokens.weight = torch.nn.Parameter(torch.empty(token_num, tokem_dim, device=model.device, dtype=model.dtype))

            print('Loading additional LLaVA weights...')
            if os.path.exists(os.path.join(model_path, 'non_lora_trainables.bin')):
                non_lora_trainables = torch.load(os.path.join(model_path, 'non_lora_trainables.bin'), map_location='cpu')
            else:
                # this is probably from HF Hub
                from huggingface_hub import hf_hub_download
                def load_from_hf(repo_id, filename, subfolder=None):
                    cache_file = hf_hub_download(
                        repo_id=repo_id,
                        filename=filename,
                        subfolder=subfolder)
                    return torch.load(cache_file, map_location='cpu')
                non_lora_trainables = load_from_hf(model_path, 'non_lora_trainables.bin')
            non_lora_trainables = {(k[11:] if k.startswith('base_model.') else k): v for k, v in non_lora_trainables.items()}
            if any(k.startswith('model.model.') for k in non_lora_trainables):
                non_lora_trainables = {(k[6:] if k.startswith('model.') else k): v for k, v in non_lora_trainables.items()}
            model.load_state_dict(non_lora_trainables, strict=False)

            from peft import PeftModel
            print('Loading LoRA weights...')
            model = PeftModel.from_pretrained(model, model_path)
            print('Merging LoRA weights...')
            model = model.merge_and_unload()
            print('Model is loaded...')
        elif model_base is not None:
            # this may be mm projector only
            print('Loading LLaVA from base model...')
            if 'mpt' in model_name.lower():
                if not os.path.isfile(os.path.join(model_path, 'configuration_mpt.py')):
                    shutil.copyfile(os.path.join(model_base, 'configuration_mpt.py'), os.path.join(model_path, 'configuration_mpt.py'))
                tokenizer = AutoTokenizer.from_pretrained(model_base, use_fast=True)
                cfg_pretrained = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
                model = LlavaMPTForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=cfg_pretrained, **kwargs)
            # =============================================================================================
            elif 'qwen' in model_name.lower():
                tokenizer = AutoTokenizer.from_pretrained(model_base, use_fast=False)
                cfg_pretrained = AutoConfig.from_pretrained(model_path)
                if getattr(cfg_pretrained, 'moe_enable', False):
                    model = EvalMoELLaVAQWenForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=cfg_pretrained, **kwargs)
                    import deepspeed
                    deepspeed.init_distributed(dist_backend='nccl', auto_mpi_discovery=False)
                    # Initialize the DeepSpeed-Inference engine
                    ds_engine = deepspeed.init_inference(model,
                                                         # mp_size=2,
                                                         # dtype=torch.half,
                                                         checkpoint=None,
                                                         replace_with_kernel_inject=True)
                    model = ds_engine.module
                else:
                    model = LlavaQWenForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=cfg_pretrained, **kwargs)
                model.generation_config = GenerationConfig.from_pretrained(model_base, pad_token_id=tokenizer.pad_token_id)
            else:
                tokenizer = AutoTokenizer.from_pretrained(model_base, use_fast=False)
                cfg_pretrained = AutoConfig.from_pretrained(model_path)
                if getattr(cfg_pretrained, 'moe_enable', False):
                    model = EvalMoELLaVALlamaForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=cfg_pretrained, **kwargs)
                    import deepspeed
                    deepspeed.init_distributed(dist_backend='nccl', auto_mpi_discovery=False)
                    # Initialize the DeepSpeed-Inference engine
                    ds_engine = deepspeed.init_inference(model,
                                                         # mp_size=2,
                                                         # dtype=torch.half,
                                                         checkpoint=None,
                                                         replace_with_kernel_inject=True)
                    model = ds_engine.module
                else:
                    model = LlavaLlamaForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, config=cfg_pretrained, **kwargs)
            # =============================================================================================

            mm_projector_weights = torch.load(os.path.join(model_path, 'mm_projector.bin'), map_location='cpu')
            mm_projector_weights = {k: v.to(torch.float16) for k, v in mm_projector_weights.items()}
            model.load_state_dict(mm_projector_weights, strict=False)
        else:
            if 'mpt' in model_name.lower():
                if 'moe' in model_name.lower():  # TODO: adapt to moe
                    raise NotImplementedError
                else:
                    tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True)
                    model = LlavaMPTForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, **kwargs)
            elif 'qwen' in model_name.lower():
                tokenizer = QWenTokenizer.from_pretrained(model_path, use_fast=False)
                if 'moe' in model_name.lower():
                    assert not load_8bit and not load_4bit  # FIXME
                    model = EvalMoELLaVAQWenForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, **kwargs)
                    import deepspeed
                    deepspeed.init_distributed(dist_backend='nccl', auto_mpi_discovery=False)
                    # Initialize the DeepSpeed-Inference engine
                    ds_engine = deepspeed.init_inference(model,
                                                         # mp_size=2,
                                                         dtype=torch.half,
                                                         checkpoint=None,
                                                         replace_with_kernel_inject=True)
                    model = ds_engine.module
                else:
                    model = LlavaQWenForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, **kwargs)
                model.generation_config = GenerationConfig.from_pretrained(model_path, pad_token_id=tokenizer.pad_token_id)
            else:
                tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=False)
                if 'moe' in model_name.lower():
                    assert not load_8bit and not load_4bit  # FIXME
                    model = EvalMoELLaVALlamaForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, **kwargs)
                    import deepspeed
                    deepspeed.init_distributed(dist_backend='nccl', auto_mpi_discovery=False)
                    # Initialize the DeepSpeed-Inference engine
                    ds_engine = deepspeed.init_inference(model,
                                                         # mp_size=2,
                                                         # dtype=torch.half,
                                                         checkpoint=None,
                                                         replace_with_kernel_inject=True)
                    model = ds_engine.module
                else:
                    model = LlavaLlamaForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, **kwargs)

    else:
        # Load language model
        if model_base is not None:
            # PEFT model
            from peft import PeftModel
            tokenizer = AutoTokenizer.from_pretrained(model_base, use_fast=False)
            model = AutoModelForCausalLM.from_pretrained(model_base, low_cpu_mem_usage=True, **kwargs)
            print(f"Loading LoRA weights from {model_path}")
            model = PeftModel.from_pretrained(model, model_path)
            print(f"Merging weights")
            model = model.merge_and_unload()
            print('Convert to FP16...')
            model.to(torch.float16)
        else:
            use_fast = False
            if 'mpt' in model_name.lower():
                tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=True)
                model = AutoModelForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, trust_remote_code=True, **kwargs)
            else:
                tokenizer = AutoTokenizer.from_pretrained(model_path, use_fast=False)
                model = AutoModelForCausalLM.from_pretrained(model_path, low_cpu_mem_usage=True, **kwargs)

    # ==========================================================================================================
    processor = {'image': None, 'video': None}

    # import ipdb
    # ipdb.set_trace()
    if 'llava' in model_name.lower():
        mm_use_im_start_end = getattr(model.config, "mm_use_im_start_end", False)
        mm_use_im_patch_token = getattr(model.config, "mm_use_im_patch_token", True)
        if mm_use_im_patch_token:
            tokenizer.add_tokens([DEFAULT_IMAGE_PATCH_TOKEN], special_tokens=True)
            tokenizer.add_tokens([DEFAULT_VIDEO_PATCH_TOKEN], special_tokens=True)
        if mm_use_im_start_end:
            tokenizer.add_tokens([DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN], special_tokens=True)
            tokenizer.add_tokens([DEFAULT_VID_START_TOKEN, DEFAULT_VID_END_TOKEN], special_tokens=True)
        model.resize_token_embeddings(len(tokenizer))

        if model.config.mm_image_tower is not None:
            image_tower = model.get_image_tower()
            if not image_tower.is_loaded:
                image_tower.load_model()
            image_tower.to(device=device, dtype=torch.float16)
            image_processor = image_tower.image_processor
            processor['image'] = image_processor

        if model.config.mm_video_tower is not None:
            video_tower = model.get_video_tower()
            if not video_tower.is_loaded:
                video_tower.load_model()
            video_tower.to(device=device, dtype=torch.float16)
            video_processor = video_tower.video_processor
            processor['video'] = video_processor

    # ==========================================================================================================
    if hasattr(model.config, "max_sequence_length"):
        context_len = model.config.max_sequence_length
    else:
        context_len = 2048

    return tokenizer, model, processor, context_len
