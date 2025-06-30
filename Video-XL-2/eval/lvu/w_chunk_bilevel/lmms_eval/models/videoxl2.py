import math

import torch
from accelerate import Accelerator, DistributedType, InitProcessGroupKwargs
from accelerate.state import AcceleratorState
from transformers import AutoConfig

torch.backends.cuda.matmul.allow_tf32 = True

import copy
import logging
import warnings
from datetime import timedelta
from typing import List, Optional, Tuple, Union
import json
import numpy as np
import re
import os
import PIL
from PIL import Image
import pdb
from decord import VideoReader, cpu
from packaging import version
from tqdm import tqdm
import cv2
warnings.filterwarnings("ignore")

eval_logger = logging.getLogger("lmms-eval")

from lmms_eval import utils
from lmms_eval.api.instance import Instance
from lmms_eval.api.model import lmms
from lmms_eval.api.registry import register_model

from videoxl2.videoxl2.constants import (
     DEFAULT_IM_END_TOKEN,
     DEFAULT_IM_START_TOKEN,
     DEFAULT_IMAGE_TOKEN,
     IGNORE_INDEX,
     IMAGE_TOKEN_INDEX,
)
from videoxl2.videoxl2.conversation import SeparatorStyle, conv_templates
from videoxl2.videoxl2.model.builder import load_pretrained_model
from videoxl2.videoxl2.mm_utils import KeywordsStoppingCriteria,get_model_name_from_path,process_images,tokenizer_image_token,transform_input_id,process_images_mvbench

# inference implementation for attention, can be "sdpa", "eager", "flash_attention_2". Seems FA2 is not effective during inference: https://discuss.huggingface.co/t/flash-attention-has-no-effect-on-inference/73453/5
# if is_flash_attn_2_available:
#     best_fit_attn_implementation = "flash_attention_2" # flash_attn has a bug that says: ERROR Error query and key must have the same dtype in generating

if version.parse(torch.__version__) >= version.parse("2.1.2"):
    best_fit_attn_implementation = "sdpa"
else:
    best_fit_attn_implementation = "eager"


best_fit_attn_implementation = "flash_attention_2"
@register_model("videoxl")
class Videoxl2(lmms):
    def __init__(
        self,
        pretrained: str = None,
        truncation: Optional[bool] = True,
        device: Optional[str] = "cuda:0",
        batch_size: Optional[Union[int, str]] = 1,
        model_name: Optional[str] = None,
        attn_implementation: Optional[str] = best_fit_attn_implementation,
        device_map: Optional[str] = "cuda:0",
        conv_template: Optional[str] = "vicuna_v1",
        use_cache: Optional[bool] = True,
        truncate_context: Optional[bool] = False,  # whether to truncate the context in generation, set it False for LLaVA-1.6
        customized_config: Optional[str] = None,  # ends in json
        max_frames_num: Optional[int] = 32,
        fps: Optional[int] = 1,
        max_fps: Optional[int] = None,
        mm_spatial_pool_stride: Optional[int] = 2,
        mm_spatial_pool_mode: Optional[str] = "average",
        token_strategy: Optional[str] = "single",  # could be "single" or "multiple", "multiple" denotes adding multiple <image> tokens for each frame
        video_decode_backend: str = "pyav",
        search_gt_chunk_idx_path=None,
        block_size_chosed=None,
        prev_blocks_num=None,
        retriev_agrs: Optional[str] = "3;4;4",
        selected_info_file_path: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        # Do not use kwargs for now
        assert kwargs == {}, f"Unexpected kwargs: {kwargs}"

        self.benchmark = os.path.basename(selected_info_file_path).split('.json')[0]
        with open(selected_info_file_path, 'r') as f:
            self.selected_info = json.load(f)
        self.selected_unit_info_aggr = self.get_mapping_dict(self.selected_info)

        self.prev_blocks_num = prev_blocks_num
        self.block_size_chosed = block_size_chosed

        accelerator_kwargs = InitProcessGroupKwargs(timeout=timedelta(weeks=52))
        accelerator = Accelerator(kwargs_handlers=[accelerator_kwargs])
        self.accelerator = accelerator
        if accelerator.num_processes > 1:
            self._device = torch.device(f"cuda:{accelerator.local_process_index}")
            self.device_map = f"cuda:{accelerator.local_process_index}"
        elif accelerator.num_processes == 1 and device_map == "auto":
            self._device = torch.device(device)
            self.device_map = device_map
        else:
            self._device = torch.device(f"cuda:{accelerator.local_process_index}")
            self.device_map = f"cuda:{accelerator.local_process_index}"

        llava_model_args = {
            "multimodal": True,
        }
        if customized_config is not None:
            llava_model_args["customized_config"] = customized_config
        if attn_implementation is not None:
            llava_model_args["attn_implementation"] = attn_implementation
        if "use_flash_attention_2" in kwargs:
            llava_model_args["use_flash_attention_2"] = kwargs["use_flash_attention_2"]
        
        model_name = model_name if model_name is not None else get_model_name_from_path(pretrained)
        
        self.pretrained = pretrained
        self.token_strategy = token_strategy
        self.max_frames_num = max_frames_num
        self.fps = fps
        self.max_fps = max_fps
        self.mm_spatial_pool_stride = mm_spatial_pool_stride
        self.mm_spatial_pool_mode = mm_spatial_pool_mode
        self.video_decode_backend = video_decode_backend

        overwrite_config = {}
        overwrite_config["mm_spatial_pool_stride"] = self.mm_spatial_pool_stride
        overwrite_config["mm_spatial_pool_mode"] = self.mm_spatial_pool_mode
        cfg_pretrained = AutoConfig.from_pretrained(self.pretrained)

        llava_model_args["overwrite_config"] = overwrite_config
        try:
            # Try to load the model with the multimodal argument
            self._tokenizer, self._model, self._image_processor, self._max_length = load_pretrained_model(pretrained, None, model_name, device_map=self.device_map, **llava_model_args)
        except TypeError:
            # for older versions of LLaVA that don't have multimodal argument
            llava_model_args.pop("multimodal", None)
            self._tokenizer, self._model, self._image_processor, self._max_length = load_pretrained_model(pretrained, None, model_name, device_map=self.device_map, **llava_model_args)

        self._config = self._model.config
        self.model.eval()
        self.model.tie_weights()
        self.truncation = truncation
        self.batch_size_per_gpu = int(batch_size)
        self.conv_template = conv_template
        self.use_cache = use_cache
        self.truncate_context = truncate_context
        assert self.batch_size_per_gpu == 1, "Llava currently does not support batched generation. See https://github.com/haotian-liu/LLaVA/issues/754. HF Llava also has this issue."

        if accelerator.num_processes > 1:
            assert accelerator.distributed_type in [DistributedType.FSDP, DistributedType.MULTI_GPU, DistributedType.DEEPSPEED], "Unsupported distributed type provided. Only DDP and FSDP are supported."
            # If you want to use DistributedType.DEEPSPEED, you have to run accelerate config before using the model
            # Also, you have to select zero stage 0 (equivalent to DDP) in order to make the prepare model works
            # I tried to set different parameters in the kwargs to let default zero 2 stage works, but it didn't work.
            if accelerator.distributed_type == DistributedType.DEEPSPEED:
                kwargs = {
                    "train_micro_batch_size_per_gpu": self.batch_size_per_gpu,
                    "train_batch_size": self.batch_size_per_gpu * accelerator.num_processes,
                }
                AcceleratorState().deepspeed_plugin.deepspeed_config_process(must_match=True, **kwargs)
                eval_logger.info("Detected that you are using DistributedType.DEEPSPEED. Make sure you run `accelerate config` and set zero stage to 0")

            if accelerator.distributed_type == DistributedType.FSDP or accelerator.distributed_type == DistributedType.DEEPSPEED:
                self._model = accelerator.prepare(self.model)
            else:
                self._model = accelerator.prepare_model(self.model, evaluation_mode=True)
            self.accelerator = accelerator
            if self.accelerator.is_local_main_process:
                eval_logger.info(f"Using {accelerator.num_processes} devices with data parallelism")
            self._rank = self.accelerator.local_process_index
            self._world_size = self.accelerator.num_processes

        elif accelerator.num_processes == 1 and device_map == "auto":
            eval_logger.info(f"Using {accelerator.num_processes} devices with tensor parallelism")
            self._rank = 0
            self._word_size = 1

        else:
            eval_logger.info(f"Using single device: {self._device}")
            self.model.to(self._device)
            self._rank = 0
            self._world_size = 1

    def get_mapping_dict(self, selected_info):
       # turt to dict: {'unique_id': selected_info}
        aggregate_dict = {}
        for category in selected_info:
            if selected_info[category] is not None:
                selected_config = selected_info[category]['config']
                selected_unit_indices = selected_info[category]['selected_unit_indices']
                selected_unit_info = {unique_id:(selected_unit_indices[unique_id], selected_config) for unique_id in selected_unit_indices}
                aggregate_dict.update(selected_unit_info)
                
        return aggregate_dict

    @property
    def config(self):
        # return the associated transformers.AutoConfig for the given pretrained model.
        return self._config

    @property
    def tokenizer(self):
        return self._tokenizer

    @property
    def model(self):
        # returns the model, unwrapping it if using Accelerate
        if hasattr(self, "accelerator"):
            return self.accelerator.unwrap_model(self._model)
        else:
            return self._model

    @property
    def eot_token_id(self):
        # we use EOT because end of *text* is more accurate for what we're doing than end of *sentence*
        return self.tokenizer.eos_token_id

    @property
    def max_length(self):
        return self._max_length

    def pad_sequence(self, input_ids, batch_first, padding_value):
        if self.tokenizer.padding_side == "left":
            input_ids = [torch.flip(_input_ids, [0]) for _input_ids in input_ids]
        input_ids = torch.nn.utils.rnn.pad_sequence(input_ids, batch_first=batch_first, padding_value=padding_value)
        if self.tokenizer.padding_side == "left":
            input_ids = torch.flip(input_ids, [1])
        return input_ids

    @property
    def batch_size(self):
        return self.batch_size_per_gpu

    @property
    def device(self):
        return self._device
    def video_len(self,video_file):
        cap = cv2.VideoCapture(video_file)
        if not cap.isOpened():
            print(f"Error: 无法打开视频文件 {video_file}")
            return None, None

        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frame_num = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        video_duration_seconds = total_frame_num / fps if fps > 0 else 0
        return video_duration_seconds
    @property
    def rank(self):
        return self._rank

    @property
    def world_size(self):
        return self._world_size

    def tok_encode(self, string: str, left_truncate_len=None, add_special_tokens=None) -> List[int]:
        """ """
        add_special_tokens = False if add_special_tokens is None else add_special_tokens
        encoding = self.tokenizer.encode(string, add_special_tokens=add_special_tokens)
        # left-truncate the encoded context to be at most `left_truncate_len` tokens long
        if left_truncate_len:
            encoding = encoding[-left_truncate_len:]
        return encoding

    def tok_decode(self, tokens):
        try:
            return self.tokenizer.decode(tokens)
        except:
            return self.tokenizer.decode([tokens])


    def flatten(self, input):
        new_list = []
        for i in input:
            for j in i:
                new_list.append(j)
        return new_list

    def load_video_uniform(self, video_path, max_frames_num):
        if type(video_path) == str:
            vr = VideoReader(video_path, ctx=cpu(0))
        else:
            vr = VideoReader(video_path[0], ctx=cpu(0))
        total_frame_num = len(vr)
        
        fps = vr.get_avg_fps()
        
        uniform_sampled_frames = np.linspace(0, total_frame_num - 1, max_frames_num, dtype=int)
        frame_idx = uniform_sampled_frames.tolist()
        spare_frames = vr.get_batch(frame_idx).asnumpy()

        frame_idx = uniform_sampled_frames.tolist()

        spare_frames = vr.get_batch(frame_idx).asnumpy()

        timestamps = [round(frame_index / fps, 1) for frame_index in frame_idx]
        #print(timestamps)
        return spare_frames,timestamps
    

    def load_video(self, video_path, max_frames_num, fps=1, max_fps=4):

        if isinstance(video_path, str):
            vr = VideoReader(video_path, ctx=cpu(0))
        else:
            vr = VideoReader(video_path[0], ctx=cpu(0))
        total_frame_num = len(vr)
        avg_fps_from_decord = vr.get_avg_fps()
        effective_fps = avg_fps_from_decord

        if effective_fps <= 0:
            print("Warning: Effective FPS is 0, cannot estimate timestamps.")
            return None, None, []

        video_fps = fps
        step = round(effective_fps / video_fps) if video_fps > 0 and effective_fps > 0 else 1
        frame_idx = [i for i in range(0, total_frame_num, step)]

        fps_upbound = max_fps
        frames_upbound = max_frames_num

        if fps_upbound is not None:
            higher_fps = min(frames_upbound//len(frame_idx), fps_upbound)
            if higher_fps > video_fps:
                higher_steps = round(effective_fps / higher_fps)
                frame_idx = [i for i in range(0, total_frame_num, higher_steps)]

        if frames_upbound > 0:
            if len(frame_idx) > frames_upbound:
                uniform_sampled_frames = np.linspace(0, total_frame_num - 1, frames_upbound, dtype=int)
                frame_idx = uniform_sampled_frames.tolist()

        timestamps = [round(idx / effective_fps, 1) for idx in frame_idx]
        video = vr.get_batch(frame_idx).asnumpy()
        vr.seek(0)
        return video, timestamps

    def load_video_uniform(self, video_path, max_frames_num, fps=None, max_fps=None):
        if type(video_path) == str:
            vr = VideoReader(video_path, ctx=cpu(0))
        else:
            vr = VideoReader(video_path[0], ctx=cpu(0))
        total_frame_num = len(vr)
        
        avg_fps_from_decord = vr.get_avg_fps()
        
        uniform_sampled_frames = np.linspace(0, total_frame_num - 1, max_frames_num, dtype=int)
        frame_idx = uniform_sampled_frames.tolist()
        spare_frames = vr.get_batch(frame_idx).asnumpy()

        frame_idx = uniform_sampled_frames.tolist()

        spare_frames = vr.get_batch(frame_idx).asnumpy()

        timestamps = [round(frame_index / avg_fps_from_decord, 1) for frame_index in frame_idx]
        #print(timestamps)
        return spare_frames,timestamps
    

    def mmss_to_seconds(self,time_str):
        """将 mm:ss 格式的时间转换为秒"""
        minutes, seconds = map(int, time_str.split(':'))
        return minutes * 60 + seconds

    def convert_time_in_prompt(self,prompt):
        """转换 prompt 中的时间表示"""
        # 匹配 mm:ss 格式的时间
        res=[]
        for promp in prompt:
            time_pattern = re.compile(r'(\d{2}:\d{2})')

            # 查找所有时间并转换为秒
            times = time_pattern.findall(promp)
            times_in_seconds = [self.mmss_to_seconds(t) for t in times]

            # 替换原时间
            converted_prompt = promp
            for original, seconds in zip(times, times_in_seconds):
                converted_prompt = converted_prompt.replace(original, f"{seconds} seconds")
            res.append(converted_prompt)
            
        return res
    def get_selected_unit_indices(self, inp, video_path):

        video_name = os.path.basename(video_path)
        # get unique_id from inp:
        if 'videomme' in self.benchmark:
            question = inp.split('\n[')[0].split('option.\n')[-1]
            if 'This video\'s subtitles are listed below:' in inp:
                unique_id = video_name + '_' + inp.split('Respond with only the letter (A, B, C, or D) of the correct option.\n')[-1].split('\n[')[0]
            else:
                unique_id = video_name + '_' + inp.split('\n[')[0].split('option.\n')[-1]
        elif 'mlvu_test' in self.benchmark or 'mlvu' in self.benchmark:
            unique_id = video_name + '_' + inp.split('\n(')[0].strip()
        elif 'vnbench' in self.benchmark or 'longvideobench' in self.benchmark:
            unique_id = video_name + '_' + inp.split('\nA.')[0]
        elif 'lvbench' in self.benchmark:
            unique_id = video_name.split('.mp4')[0] + '_' + inp.split('\nA.')[0]

        if unique_id in self.selected_unit_info_aggr:
            selected_unit_indices, selected_config = self.selected_unit_info_aggr[unique_id]
        else:
            selected_unit_indices = None
            selected_config = None

        return selected_unit_indices, selected_config

    def generate_until(self, requests: List[Instance]) -> List[str]:
        res = []

        def _collate(x):
            # the negative sign on len(toks) sorts descending - this has a few advantages:
            # - time estimates will always be over not underestimates, which is more useful for planning
            # - to know the size of a batch when going through the list, you know the first one is always the batch
            #   padded context length. this is useful to simplify the batching logic and more importantly to make
            #   automatic adaptive batches much much easier to implement
            # - any OOMs will happen right away rather than near the end
            toks = self.tok_encode(x[0])
            return -len(toks), x[0]

        # we group requests by their generation_kwargs,
        # so that we don't try to execute e.g. greedy sampling and temp=0.8 sampling
        # in the same batch.
        re_ords = utils.Collator([reg.args for reg in requests], _collate, grouping=True)
        chunks = re_ords.get_batched(n=self.batch_size, batch_fn=None)
        num_iters = len(requests) // self.batch_size if len(requests) % self.batch_size == 0 else len(requests) // self.batch_size + 1
        pbar = tqdm(total=num_iters, disable=(self.rank != 0), desc="Model Responding")
        for chunk in chunks:
            batched_contexts, all_gen_kwargs, batched_doc_to_visual, batched_doc_id, batched_task, batched_split = zip(*chunk)
            task = batched_task[0]
            split = batched_split[0]
            batched_visuals = [batched_doc_to_visual[0](self.task_dict[task][split][ids]) for ids in batched_doc_id]  # [B, N]
            flattened_visuals = self.flatten(batched_visuals)  # [B*N]
            assert len(batched_visuals) == 1

            self.selected_unit_info_aggr
            selected_unit_indices, selected_config = self.get_selected_unit_indices(batched_contexts[0], flattened_visuals[0])
           
            # we assume all gen kwargs in the batch are the same
            # this is safe to assume because the `grouper` object ensures it.
            gen_kwargs = all_gen_kwargs[0]
            if "until" in gen_kwargs:
                gen_kwargs.pop("until")

            question_input = []
            res_cans=[self.task_dict[task][split][ids] for ids in batched_doc_id]

            for visual, context,res_can in zip(batched_visuals, batched_contexts,res_cans):
                video_length=-1
                
                if "image_aspect_ratio" in gen_kwargs.keys() and "image_aspect_ratio" not in self._config.__dict__:
                    # here we should pop it out of gen_kwargs so that it doesn't get passed to the model for next step of generation
                    self._config.image_aspect_ratio = gen_kwargs.pop("image_aspect_ratio")
                    eval_logger.info(f"Setting image aspect ratio: {self._config.image_aspect_ratio}")

                if type(visual[0]) == PIL.Image.Image:  # For image task
                    time_stamps=[]
                    image_tensor = process_images_mvbench(visual, self._image_processor, self._config)

                    if type(image_tensor) is list:
                        image_tensor = [_image.to(dtype=torch.float16, device=self.device) for _image in image_tensor]
                    else:
                        image_tensor = image_tensor.to(dtype=torch.float16, device=self.device)
                    
                    times= [float(i) for i in range(0,len(image_tensor))]
                    
                    token_frames_sum=(len(times)+3)//4
                    compress_frame = times[::4]
                    time_embedding = []
                    for time in compress_frame:
                        item = f"Time {time}s:"
                        time_embedding.append(self.tokenizer(item).input_ids)
                        time_embedding.append([151654]*144)
                                
                    time_embedding = [item for sublist in time_embedding for item in sublist]

                    time_embedding = torch.tensor(time_embedding, dtype=torch.long).to(self.device)
                    time_stamps.append(time_embedding)
                    image_tensor=[image_tensor]
                    task_type = "video"

                elif type(visual[0]) == str:  # For video task
                    image_tensor = []
                    time_stamps=[]
                    try:
                        video_length=self.video_len(visual[0])
                        if self.video_decode_backend == "decord":
                            frames,times= self.load_video(visual, self.max_frames_num, fps=self.fps, max_fps=self.max_fps)
                            token_frames_sum=(len(times)+3)//4
                            compress_frame = times[::4]
                            time_embedding = []
                            for time in compress_frame:
                                item = f"Time {time}s:"
                                time_embedding.append(self.tokenizer(item).input_ids)
                                time_embedding.append([151654]*144)

                            time_embedding = [item for sublist in time_embedding for item in sublist]

                            time_embedding = torch.tensor(time_embedding, dtype=torch.long).to(self.device)
                            time_stamps.append(time_embedding)
                            
                        frames = self._image_processor.preprocess(frames, return_tensors="pt")["pixel_values"].to(self.device, dtype=torch.float16)
                        image_tensor.append(frames)
                    except Exception as e:
                        eval_logger.error(f"Error {e} in loading video")
                        image_tensor = None
                        
                    #print(len(image_tensor),len(time_stamps))
                    task_type = "video"
                    
                if image_tensor is not None and len(image_tensor) != 0 and DEFAULT_IMAGE_TOKEN not in context:
                    """
                    Three senarios:
                    1. No image, and there for, no image token should be added.
                    2. image token is already specified in the context, so we don't need to add it.
                    3. image token is not specified in the context and there is image inputs, so we need to add it. In this case, we add the image token at the beginning of the context and add a new line.
                    4. For video tasks, we could add a <image> token or multiple <image> tokens for each frame in the context. This depends on the training strategy and should balance in test to decide which is better
                    """
                    if task_type == "image":
                        image_tokens = [DEFAULT_IMAGE_TOKEN] * len(visual) if isinstance(visual, list) else [DEFAULT_IMAGE_TOKEN]
                    elif task_type == "video":
                        image_tokens = [DEFAULT_IMAGE_TOKEN] * len(frames) if self.token_strategy == "multiple" else [DEFAULT_IMAGE_TOKEN]

                    image_tokens = " ".join(image_tokens)
                    question = image_tokens + "\n" + context
                else:
                    question = context
                
                # This is much safer for llama3, as we now have some object type in it
                if "llama_3" in self.conv_template:
                    conv = copy.deepcopy(conv_templates[self.conv_template])
                else:
                    conv = conv_templates[self.conv_template].copy()

                conv.append_message(conv.roles[0], question)
                conv.append_message(conv.roles[1], None)
                prompt_question = conv.get_prompt()
                question_input.append(prompt_question)
            # preconfigure gen_kwargs with defaults
            if "max_new_tokens" not in gen_kwargs:
                gen_kwargs["max_new_tokens"] = 1024
            if "temperature" not in gen_kwargs:
                gen_kwargs["temperature"] = 0
            if "do_sample" not in gen_kwargs:
                gen_kwargs["do_sample"] = False
            if "top_p" not in gen_kwargs:
                gen_kwargs["top_p"] = None
            if "num_beams" not in gen_kwargs:
                gen_kwargs["num_beams"] = 1

            question_input = self.convert_time_in_prompt(question_input)
            input_ids_list = [tokenizer_image_token(prompt, self.tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt") for prompt in question_input]
            
            pad_token_ids = self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else self.tokenizer.eos_token_id
            input_ids = self.pad_sequence(input_ids_list, batch_first=True, padding_value=pad_token_ids).to(self.device)
            
            
            attention_masks = input_ids.ne(pad_token_ids).to(self.device)

            if task_type == "image":
                gen_kwargs["image_sizes"] = [flattened_visuals[idx].size for idx in range(len(flattened_visuals))]
            elif task_type == "video":
                stop_str = conv.sep if conv.sep_style != SeparatorStyle.TWO else conv.sep2
                keywords = [stop_str]
                stopping_criteria = KeywordsStoppingCriteria(keywords, self.tokenizer, input_ids)
                gen_kwargs["modalities"] = ["video"]
                gen_kwargs["stopping_criteria"] = [stopping_criteria]
                self._config.mm_spatial_pool_stride = self.mm_spatial_pool_stride
                self._config.mm_spatial_pool_mode = self.mm_spatial_pool_mode

            if "image_aspect_ratio" in gen_kwargs.keys():
                gen_kwargs.pop("image_aspect_ratio")
            try:

                with torch.inference_mode():
                    output_ids = self.model.generate(input_ids, images=image_tensor,time_embedding=time_stamps,  prev_blocks_num=self.prev_blocks_num, block_size_chosed=self.block_size_chosed, selected_unit_indices=selected_unit_indices, selected_config=selected_config, **gen_kwargs)
                
                text_outputs = self.tokenizer.batch_decode(output_ids, skip_special_tokens=True)
            except Exception as e:
                raise e

            text_outputs = [response.strip() for response in text_outputs]
            res.extend(text_outputs)
            self.cache_hook.add_partial("generate_until", (context, gen_kwargs), text_outputs)
            pbar.update(1)
        res = re_ords.get_original(res)
        pbar.close()
        return res
    
    def loglikelihood(self, requests: List[Instance]) -> List[Tuple[float, bool]]:
        
        # TODO
        res = []
        pbar = tqdm(total=len(requests), disable=(self.rank != 0), desc="Model Responding")

        for contexts, doc_to_target, doc_to_visual, doc_id, task, split in [reg.args for reg in requests]:
            # encode, pad, and truncate contexts for this batch
            if type(doc_to_target) == str:
                continuation = doc_to_target
            else:
                continuation = doc_to_target(self.task_dict[task][split][doc_id])
            visuals = [doc_to_visual(self.task_dict[task][split][doc_id])]
            visuals = self.flatten(visuals)
            image_sizes = [[visual.size[0], visual.size[1]] for visual in visuals]
            if visuals:
                image = process_images(visuals, self._image_processor, self._config)
                if type(image) is list:
                    image = [_image.to(dtype=torch.float16, device=self.device) for _image in image]
                else:
                    image = image.to(dtype=torch.float16, device=self.device)
            else:
                image = None

            prompts_input = contexts[0] if isinstance(contexts, list) else contexts

            if image is not None and len(image) != 0 and DEFAULT_IMAGE_TOKEN not in prompts_input:
                """
                Three senarios:
                1. No image, and there for, no image token should be added.
                2. image token is already specified in the context, so we don't need to add it.
                3. image token is not specified in the context and there is image inputs, so we need to add it. In this case, we add the image token at the beginning of the context and add a new line.
                """
                image_tokens = [DEFAULT_IMAGE_TOKEN] * len(visuals)
                image_tokens = " ".join(image_tokens)
                prompts_input = image_tokens + "\n" + (contexts[0] if isinstance(contexts, list) else contexts)

            # This is much safer for llama3, as we now have some object type in it
            if "llama_3" in self.conv_template:
                conv = copy.deepcopy(conv_templates[self.conv_template])
            else:
                conv = conv_templates[self.conv_template].copy()

            conv.append_message(conv.roles[0], prompts_input)
            conv.append_message(conv.roles[1], None)
            prompt = conv.get_prompt()
            pad_token_id = self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else self.tokenizer.eos_token_id
            contxt_id = tokenizer_image_token(prompt, self.tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt").unsqueeze(0).to(self.device)
            # Add the answer of the second role
            conv.messages[1][1] = continuation

            prompt = conv.get_prompt()
            input_ids = tokenizer_image_token(prompt, self.tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt").unsqueeze(0).to(self.device)
            labels = input_ids.clone()
            # Context part no need to calculate for loss
            labels[0, : contxt_id.shape[1]] = -100
            with torch.inference_mode():
                outputs = self.model(input_ids=input_ids, labels=labels, images=image, use_cache=True, image_sizes=image_sizes)
            loss = outputs["loss"]
            # loss = torch.exp(loss)
            logits = outputs["logits"]
            greedy_tokens = logits.argmax(dim=-1)
            cont_toks = input_ids[:, contxt_id.shape[1] :]  # [1, seq]
            greedy_tokens = greedy_tokens[:, contxt_id.shape[1] : input_ids.shape[1]]  # [1, seq]
            max_equal = (greedy_tokens == cont_toks).all()
            res.append((float(loss.item()), bool(max_equal)))
            pbar.update(1)

        pbar.close()
        return res