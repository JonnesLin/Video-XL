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
from lmms_eval.models.model_utils.load_video import read_video_pyav

from transformers import T5Tokenizer, T5EncoderModel
from longva.longva.constants import (
     DEFAULT_IM_END_TOKEN,
     DEFAULT_IM_START_TOKEN,
     DEFAULT_IMAGE_TOKEN,
     IGNORE_INDEX,
     IMAGE_TOKEN_INDEX,
)
from longva.longva.conversation import SeparatorStyle, conv_templates
from longva.longva.model.builder import load_pretrained_model
from longva.longva.mm_utils import KeywordsStoppingCriteria,get_model_name_from_path,process_images,tokenizer_image_token,transform_input_id,process_images_mvbench

# inference implementation for attention, can be "sdpa", "eager", "flash_attention_2". Seems FA2 is not effective during inference: https://discuss.huggingface.co/t/flash-attention-has-no-effect-on-inference/73453/5
# if is_flash_attn_2_available:
#     best_fit_attn_implementation = "flash_attention_2" # flash_attn has a bug that says: ERROR Error query and key must have the same dtype in generating

if version.parse(torch.__version__) >= version.parse("2.1.2"):
    best_fit_attn_implementation = "sdpa"
else:
    best_fit_attn_implementation = "eager"

best_fit_attn_implementation = "flash_attention_2"
@register_model("videoxl")
class Videoxl(lmms):
    def __init__(
        self,
        pretrained: str = "lmms-lab/LongVA-7B",
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
        prev_blocks_num: Optional[int] = None,
        block_size_chosed: Optional[int] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        # Do not use kwargs for now
        assert kwargs == {}, f"Unexpected kwargs: {kwargs}"

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
        
        #self.t5_tokenizer=T5Tokenizer.from_pretrained('google-t5/t5-small')
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
        # start = time.time()
        # print(f'start process: {video_file}')
        if isinstance(video_path, str):
            vr = VideoReader(video_path, ctx=cpu(0))
        else:
            vr = VideoReader(video_path[0], ctx=cpu(0))
        total_frame_num = len(vr)
        avg_fps_from_decord = vr.get_avg_fps()
        # 使用用户提供的 video_fps，如果它大于 0，否则使用 decord 提供的平均帧率
        effective_fps = avg_fps_from_decord

        # 如果 effective_fps 仍然是 0，我们无法进行时间戳估算，返回空列表或采取其他策略
        if effective_fps <= 0:
            print("Warning: Effective FPS is 0, cannot estimate timestamps.")
            return None, None, []

        video_fps = fps
        # 根据平均帧率计算帧索引
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

        # 获取选中的帧和估算的时间戳
        timestamps = [round(idx / effective_fps, 1) for idx in frame_idx]

        video = vr.get_batch(frame_idx).asnumpy()
        vr.seek(0)
        return video, timestamps


    def load_video_imgs(self, video_path, max_frames_num, fps=1, max_fps=4):
        if isinstance(video_path, str):
            video_path=video_path
        else:
            video_path=video_path[0]
        lvbench_frames_dir = '/share/minghao/Datasets/VideoDatasets/lvbench_600_4fps'
        video_name = os.path.basename(video_path)
        imgs_dir = os.path.join(lvbench_frames_dir, video_name)

        if not os.path.exists(imgs_dir):
            print(f'没有 {imgs_dir}')
            return self.load_video(video_path, max_frames_num, fps=1, max_fps=4)
        
        # List all image files and sort them numerically
        frame_filenames = sorted([f for f in os.listdir(imgs_dir) if f.endswith(('.png', '.jpg', '.jpeg'))])

        if not frame_filenames:
            print(f"No image files found in '{imgs_dir}'.")
            return None

        frame_list = []
        for filename in frame_filenames:
            frame_path = os.path.join(imgs_dir, filename)
            try:
                # Using Pillow to read images
                img = Image.open(frame_path)
                # Convert PIL Image to NumPy array. Decord typically outputs (H, W, C) for frames.
                frame_array = np.array(img)
                frame_list.append(frame_array)
            except Exception as e:
                print(f"Could not read frame {filename}: {e}")
                continue

        if not frame_list:
            print("No frames were successfully loaded.")
            return None

        # Stack all individual frame arrays.
        # This will result in a (num_frames, height, width, channels) NumPy array,
        # which is consistent with decord's output for a batch of frames.
        video = np.stack(frame_list)

        vr = VideoReader(video_path, ctx=cpu(0))

        total_frame_num = len(vr)
        avg_fps_from_decord = vr.get_avg_fps()
        # 使用用户提供的 video_fps，如果它大于 0，否则使用 decord 提供的平均帧率
        effective_fps = avg_fps_from_decord

        frames_upbound = max_frames_num
        uniform_sampled_frames = np.linspace(0, total_frame_num - 1, frames_upbound, dtype=int)
        frame_idx = uniform_sampled_frames.tolist()

        # 获取选中的帧和估算的时间戳
        timestamps = [round(idx / effective_fps, 1) for idx in frame_idx]
        return video, timestamps


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

            # we assume all gen kwargs in the batch are the same
            # this is safe to assume because the `grouper` object ensures it.
            gen_kwargs = all_gen_kwargs[0]
            if "until" in gen_kwargs:
                gen_kwargs.pop("until")

            question_input = []
            #text_select= []
            res_cans=[self.task_dict[task][split][ids] for ids in batched_doc_id]
                #print(i['answer'],i['candidates'])
            
            # Dataset({
            #     features: ['candidates', 'answer', 'end', 'subtitle', 'question', 'video', 'fps', 'show_name', 'start'],
            #     num_rows: 200
            # })
            
            
            for visual, context,res_can in zip(batched_visuals, batched_contexts,res_cans):
                video_length=-1
                #text_select.append(context)
                
                if "image_aspect_ratio" in gen_kwargs.keys() and "image_aspect_ratio" not in self._config.__dict__:
                    # here we should pop it out of gen_kwargs so that it doesn't get passed to the model for next step of generation
                    self._config.image_aspect_ratio = gen_kwargs.pop("image_aspect_ratio")
                    eval_logger.info(f"Setting image aspect ratio: {self._config.image_aspect_ratio}")
                # print("########",visual)
                # encode, pad, and truncate contexts for this batch
                if type(visual[0]) == PIL.Image.Image:  # For image task
                    #print(2)
                    #print(1)
                    #image_tensor = process_images(visual, self._image_processor, self._config)
                    time_stamps=[]
                    image_tensor = process_images_mvbench(visual, self._image_processor, self._config)

                    if type(image_tensor) is list:
                        image_tensor = [_image.to(dtype=torch.float16, device=self.device) for _image in image_tensor]
                    else:
                        image_tensor = image_tensor.to(dtype=torch.float16, device=self.device)
                    #print(image_tensor.shape)
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
                    #print(len(image_tensor),len(time_stamps))
                    task_type = "video"

                elif type(visual[0]) == str:  # For video task
                    #print(1)
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
                            #print(times)

                        elif self.video_decode_backend == "imgs":
                            frames,times= self.load_video_imgs(visual, self.max_frames_num, fps=self.fps, max_fps=self.max_fps)
                            
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
                            #print(times)

                        elif self.video_decode_backend == "pyav":
                            #print(2)
                            frames,times = read_video_pyav(visual[0], num_frm=self.max_frames_num)
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
                
#                 if video_length!=-1:
#                     time_prompt=f"Video length: {round(video_length,2)} seconds, Sampled {len(frames)} frames. "
#                     question=question.replace("<image>\n", f"<image>\n{time_prompt}")
                
                    #print(question)
                # This is much safer for llama3, as we now have some object type in it
                if "llama_3" in self.conv_template:
                    conv = copy.deepcopy(conv_templates[self.conv_template])
                else:
                    conv = conv_templates[self.conv_template].copy()
                #print(question)
                if 'Information ' in question:
                    question+='\nOnly give the best option.'

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
            #print(question_input)
            question_input = self.convert_time_in_prompt(question_input)
            # print(question_input)
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
                #print(len(image_tensor),len(time_stamps))
                with torch.inference_mode():
                    output_ids = self.model.generate(input_ids, images=image_tensor,time_embedding=time_stamps,prev_blocks_num=self.prev_blocks_num, block_size_chosed=self.block_size_chosed,**gen_kwargs)
                
                text_outputs = self.tokenizer.batch_decode(output_ids, skip_special_tokens=True)
                # print(f'output_ids: {output_ids}, text_outputs: {text_outputs}')
                print(f'text_outputs: {text_outputs}')
                #text_outputs = self.tokenizer.batch_decode(cont, skip_special_tokens=True)
            except Exception as e:
                raise e

            text_outputs = [response.strip() for response in text_outputs]

            # print(res_can)
            # option_letters = ['A', 'B', 'C', 'D','E','F','G']  # 固定字母表
            # answer_idx = res_can['candidates'].index(res_can['answer'])  # 找到正确答案的索引
            # print(res_can)
            # import pdb
            # pdb.set_trace()
            # gt_option = res_can['answer']  # 映射到字母（如 2 → 'C'）
            #print(gt_option,text_outputs)
            
            # with open(str(self.device)+'mvbench.txt', 'a+') as f:
            #     f.write(gt_option)
            #     f.write('*********')
            #     f.write(str(text_outputs[0]))
            #     f.write('\n')
                

            res.extend(text_outputs)
            self.cache_hook.add_partial("generate_until", (context, gen_kwargs), text_outputs)
            pbar.update(1)
            # self.model.memory.reset()
            # reorder this group of results back to original unsorted form
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