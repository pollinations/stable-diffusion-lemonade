import sys

sys.path.append("/CLIP")
sys.path.append("/taming-transformers")
sys.path.append("/k-diffusion")
# Slightly modified version of: https://github.com/CompVis/stable-diffusion/blob/main/scripts/txt2img.py
import os
import sys
import time
# Code to turn kwargs into Jupyter widgets
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from glob import glob
from typing import List

import numpy as np
import torch
from cog import BasePredictor, Input, Path
from einops import rearrange, repeat
from googletrans import Translator
from helpers import sampler_fn, save_samples
from k_diffusion import sampling
from k_diffusion.external import CompVisDenoiser
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from ldm.util import instantiate_from_config
from omegaconf import OmegaConf
from PIL import Image
from pytorch_lightning import seed_everything
from scripts.txt2img import chunk, load_model_from_config
from torch import autocast
#from tqdm.auto import tqdm, trange  # NOTE: updated for notebook
from tqdm import tqdm, trange  # NOTE: updated for notebook


class Predictor(BasePredictor):


    @torch.inference_mode()
    def setup(self):
        self.device = (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )
        # load bad_prompt.pt and print it's shape
        self.bad_prompt = torch.load("./bad_prompt.pt")
        print("bad_prompt_shape", self.bad_prompt['string_to_param']['*'].shape)

        options = get_default_options()
        self.options = options

        self.options['ckpt'] ="/stable-diffusion-checkpoints/nitroDiffusion-v1.ckpt"
        self.model_nitrosocke = load_model(self.options, self.device)
        self.model_wrap_nitrosocke = CompVisDenoiser(self.model_nitrosocke)
        # self.options['ckpt'] ="/stable-diffusion-checkpoints/v1-5-pruned-emaonly.ckpt"
        # self.model_vanilla = load_model(self.options, self.device)
        # self.model_wrap_vanilla = CompVisDenoiser(self.model_vanilla)
        os.system("nvidia-smi")


        self.translator= Translator()

    @torch.inference_mode()
    def predict(
        self,
        prompts: str = Input(
            default="modern disney - bearded guy with a mohawk\narcher - bearded guy with a mohawk",
            description="model will try to generate this text. New! Write in any language.",
        ),
        negative_prompt: str = Input(
            default="",
            description="model will try to generate this text. New! Write in any language.",
        ),
        # model: str = Input(
        #     description='stable diffusion model. nitrosocke was fine-tuned and is better at certain styles',
        #     default='vanilla',
        #     choices=['vanilla', 'nitrosocke']        
        # ),  
        prompt_scale: float = Input(
            default=15.0,
            description="Determines influence of your prompt on generation.",
        ),
        num_frames_per_prompt: int = Input(
            default=3,
            description="Number of frames to generate per prompt (limited to a maximum of 15 for now because we are experiencing heavy use).",
        ),
        random_seed: int = Input(
            default=42,
            description="Each seed generates a different image",
        ),
        diffusion_steps: int = Input(
            default=25,
            description="Number of diffusion steps. Higher steps could produce better results but will take longer to generate. Maximum 30 (using K-Euler-Diffusion).",
        ),
        width: int = Input(
            default=512,
            description="Width of the generated image. The model was really only trained on 512x512 images. Other sizes tend to create less coherent images.",
        ),
        height: int = Input(
            default=512,
            description="Height of the generated image. The model was really only trained on 512x512 images. Other sizes tend to create less coherent images.",
        ),
        init_image: Path = Input(
            default=None, 
            description="input image"),
        init_image_strength: float = Input(
            default=0.4,
            description="How strong to apply the input image. 0 means disregard the input image mostly and 1 copies the image exactly. Values in between are interesting.")
    ) -> List[Path]:
        diffusion_steps = abs(diffusion_steps)
        num_frames_per_prompt = abs(num_frames_per_prompt)
        model = 'nitrosocke'
        if init_image is not None:
            init_image = str(init_image)
            print("using init image", init_image)
        
        options = self.options
        options['prompts'] = prompts.split("\n")
        options['prompts'] = [self.translator.translate(prompt.strip()).text for prompt in options['prompts'] if prompt.strip()]
        print("translated prompts ", options['prompts'])

        options['num_interpolation_steps'] = num_frames_per_prompt
        options['scale'] = prompt_scale
        options['seed'] = random_seed
        options['H'] = height
        options['W'] = width
        options['steps'] = diffusion_steps
        options['init_image'] = init_image
        options['init_image_strength'] = init_image_strength
        options['negative_prompt'] = negative_prompt
        
        if model == 'nitrosocke':
            model = self.model_nitrosocke
            model_wrap = self.model_wrap_nitrosocke
        else:
            model = self.model_vanilla
            model_wrap = self.model_wrap_vanilla

        run_inference(options, model, model_wrap, self.device)

        images = list(sorted(glob(f"{options['outdir']}/*.png")))
        images = [Path(image) for image in images]
        return images



def load_model(opt,device):
    """Seperates the loading of the model from the inference"""
    
    # if opt.laion400m:
    #     print("Falling back to LAION 400M model...")
    #     opt.config = "configs/latent-diffusion/txt2img-1p4B-eval.yaml"
    #     opt.ckpt = "models/ldm/text2img-large/model.ckpt"
    #     opt.outdir = "outputs/txt2img-samples-laion400m"

    config = OmegaConf.load(f"{opt.config}")
    model = load_model_from_config(config, f"{opt.ckpt}")

    model = model.to(device)
    
    return model

def slerp(t, v0, v1, DOT_THRESHOLD=0.9995):
    """ helper function to spherically interpolate two arrays v1 v2 """

    if not isinstance(v0, np.ndarray):
        inputs_are_torch = True
        input_device = v0.device
        v0 = v0.cpu().numpy()
        v1 = v1.cpu().numpy()

    dot = np.sum(v0 * v1 / (np.linalg.norm(v0) * np.linalg.norm(v1)))
    if np.abs(dot) > DOT_THRESHOLD:
        v2 = (1 - t) * v0 + t * v1
    else:
        theta_0 = np.arccos(dot)
        sin_theta_0 = np.sin(theta_0)
        theta_t = theta_0 * t
        sin_theta_t = np.sin(theta_t)
        s0 = np.sin(theta_0 - theta_t) / sin_theta_0
        s1 = sin_theta_t / sin_theta_0
        v2 = s0 * v0 + s1 * v1

    if inputs_are_torch:
        v2 = torch.from_numpy(v2).to(input_device)

    return v2

def diffuse(count_start, start_code, c, batch_size, opt, model, model_wrap, outpath, device):
    #print("diffusing with batch size", batch_size)
    uc = None
    if opt.scale != 1.0:
        negative_prompt = ""
        if opt.negative_prompt:
            negative_prompt = opt.negative_prompt
        uc = model.get_learned_conditioning(batch_size * [negative_prompt])
        # uc = model.get_learned_conditioning(negative_prompts)
        print("unconditional conditioning shape", uc.shape)

    t_enc = 0
    if opt.init_image is not None:
        t_enc = round(opt.steps * (1.0 - opt.init_image_strength))
    print("using init image", opt.init_image, "for", t_enc, "steps")
    #if args.sampler in ["klms","dpm2","dpm2_ancestral","heun","euler","euler_ancestral"]:
    samples = sampler_fn(
        c=c,
        uc=uc,
        args=opt,
        model_wrap=model_wrap,
        init_latent=start_code,
        t_enc=t_enc,
        device=device,
        # cb=callback
        )
    print("samples_ddim", samples.shape)
    x_samples = model.decode_first_stage(samples)
    x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)
    if not opt.skip_save:
        count = count_start
        for x_sample in x_samples:
            x_sample = 255. * rearrange(x_sample.cpu().numpy(), 'c h w -> h w c')
            image_path = os.path.join(outpath, f"{count:05}.png")
            prompt_path = os.path.join(outpath, f"{count:05}.txt")
            Image.fromarray(x_sample.astype(np.uint8)).save(image_path)
            count += 1
    



def run_inference(opt, model, model_wrap, device):
    """Seperates the loading of the model from the inference
    
    Additionally, slightly modified to display generated images inline
    """
    seed_everything(opt.seed)

    outpath = opt.outdir
    os.makedirs(outpath, exist_ok=True)
    os.system(("rm -rf %s/*" % outpath))

    batch_size = opt.n_samples
    prompts = opt.prompts


    print("embedding prompts")
    cs = [model.get_learned_conditioning(prompt) for prompt in prompts]

    datas = [[batch_size * c] for c in cs] 

    os.makedirs(outpath, exist_ok=True)
    
    base_count = 0
    
    start_code_a = None
    start_code_b = None
    

    if opt.init_image:
        init_image = load_img(opt.init_image, shape=(opt.W, opt.H)).to(device)
        init_image = repeat(init_image, '1 ... -> b ...', b=batch_size)

    precision_scope = autocast if opt.precision=="autocast" else nullcontext



        
    with torch.no_grad():
        with precision_scope("cuda"):
            with model.ema_scope():
                tic = time.time()
                for n in trange(opt.n_iter):
                    for data in datas:          
                        for t in np.linspace(0, 1, opt.num_interpolation_steps):

                            if opt.init_image:
                                start_code = model.get_first_stage_encoding(model.encode_first_stage(init_image))     
                            else: 
                                start_code = torch.randn([opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device)
                            for c in data:
                                diffuse(base_count, start_code, c, batch_size, opt, model, model_wrap, outpath, device)
                                base_count += 1



                toc = time.time()

    print(f"Your samples have been saved to: \n{outpath} \n"
          f" \nEnjoy.")




class WidgetDict2(OrderedDict):
    def __getattr__(self,val):
        return self[val]


def get_default_options():
    options = WidgetDict2()
    options['outdir'] ="/outputs"
    options['sampler'] = "euler_ancestral"
    options['skip_save'] = False
    options['ddim_steps'] = 50
    options['plms'] = True
    options['laion400m'] = False
    options['ddim_eta'] = 0.0
    options['n_iter'] = 1
    options['C'] = 4
    options['f'] = 8
    options['n_samples'] = 1
    options['n_rows'] = 0
    options['from_file'] = None
    options['config'] = "configs/stable-diffusion/v1-inference.yaml"
    options['precision'] = "full"  # or "full" "autocast"
    options['use_init'] = True
    # Extra option for the notebook
    options['display_inline'] = False
    options['audio_smoothing'] = 0.7
    return options


def load_img(path, shape):
    if path.startswith('http://') or path.startswith('https://'):
        image = Image.open(requests.get(path, stream=True).raw).convert('RGB')
    else:
        image = Image.open(path).convert('RGB')

    image = image.resize(shape, resample=Image.LANCZOS)
    image = np.array(image).astype(np.float32) / 255.0
    image = image[None].transpose(0, 3, 1, 2)
    image = torch.from_numpy(image)
    return 2.*image - 1.
