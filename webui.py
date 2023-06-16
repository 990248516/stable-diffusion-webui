from __future__ import annotations

import os
import sys
import time
import importlib
import signal
import re
import warnings
import json
from threading import Thread
from typing import Iterable

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from packaging import version

import logging

logging.getLogger("xformers").addFilter(lambda record: 'A matching Triton is not available' not in record.getMessage())

from modules import paths, timer, import_hook, errors  # noqa: F401

startup_timer = timer.Timer()

import torch
import pytorch_lightning   # noqa: F401 # pytorch_lightning should be imported after torch, but it re-enables warnings on import so import once to disable them
warnings.filterwarnings(action="ignore", category=DeprecationWarning, module="pytorch_lightning")
warnings.filterwarnings(action="ignore", category=UserWarning, module="torchvision")


startup_timer.record("import torch")

import gradio
startup_timer.record("import gradio")

import ldm.modules.encoders.modules  # noqa: F401
startup_timer.record("import ldm")

from modules import extra_networks
from modules.call_queue import wrap_gradio_gpu_call, wrap_queued_call, queue_lock  # noqa: F401

# Truncate version number of nightly/local build of PyTorch to not cause exceptions with CodeFormer or Safetensors
if ".dev" in torch.__version__ or "+git" in torch.__version__:
    torch.__long_version__ = torch.__version__
    torch.__version__ = re.search(r'[\d.]+[\d]', torch.__version__).group(0)

from modules import shared, sd_samplers, upscaler, extensions, localization, ui_tempdir, ui_extra_networks, config_states
import modules.codeformer_model as codeformer
import modules.face_restoration
import modules.gfpgan_model as gfpgan
import modules.img2img

import modules.lowvram
import modules.scripts
import modules.sd_hijack
import modules.sd_hijack_optimizations
import modules.sd_models
import modules.sd_vae
import modules.txt2img
import modules.script_callbacks
import modules.textual_inversion.textual_inversion
import modules.progress
import modules.hashes

import modules.ui
from modules import modelloader
from modules.shared import cmd_opts, opts, syncLock,sync_images_lock,de_register_model,get_default_sagemaker_bucket
import modules.hypernetworks.hypernetwork

from modules.paths import script_path
from huggingface_hub import hf_hub_download
import boto3
import sys
import requests
import traceback
import uuid
import psutil
import glob
FREESPACE = 20
sys.path.append(os.path.join(os.path.dirname(__file__), 'extensions/sd-webui-controlnet'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'extensions/sd_dreambooth_extension'))

if not cmd_opts.api:
    from extensions.sd_dreambooth_extension.scripts.train import train_dreambooth

startup_timer.record("other imports")


if cmd_opts.server_name:
    server_name = cmd_opts.server_name
else:
    server_name = "0.0.0.0" if cmd_opts.listen else None


def fix_asyncio_event_loop_policy():
    """
        The default `asyncio` event loop policy only automatically creates
        event loops in the main threads. Other threads must create event
        loops explicitly or `asyncio.get_event_loop` (and therefore
        `.IOLoop.current`) will fail. Installing this policy allows event
        loops to be created automatically on any thread, matching the
        behavior of Tornado versions prior to 5.0 (or 5.0 on Python 2).
    """

    import asyncio

    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        # "Any thread" and "selector" should be orthogonal, but there's not a clean
        # interface for composing policies so pick the right base.
        _BasePolicy = asyncio.WindowsSelectorEventLoopPolicy  # type: ignore
    else:
        _BasePolicy = asyncio.DefaultEventLoopPolicy

    class AnyThreadEventLoopPolicy(_BasePolicy):  # type: ignore
        """Event loop policy that allows loop creation on any thread.
        Usage::

            asyncio.set_event_loop_policy(AnyThreadEventLoopPolicy())
        """

        def get_event_loop(self) -> asyncio.AbstractEventLoop:
            try:
                return super().get_event_loop()
            except (RuntimeError, AssertionError):
                # This was an AssertionError in python 3.4.2 (which ships with debian jessie)
                # and changed to a RuntimeError in 3.4.3.
                # "There is no current event loop in thread %r"
                loop = self.new_event_loop()
                self.set_event_loop(loop)
                return loop

    asyncio.set_event_loop_policy(AnyThreadEventLoopPolicy())


def check_versions():
    if shared.cmd_opts.skip_version_check:
        return

    expected_torch_version = "2.0.0"

    if version.parse(torch.__version__) < version.parse(expected_torch_version):
        errors.print_error_explanation(f"""
You are running torch {torch.__version__}.
The program is tested to work with torch {expected_torch_version}.
To reinstall the desired version, run with commandline flag --reinstall-torch.
Beware that this will cause a lot of large files to be downloaded, as well as
there are reports of issues with training tab on the latest version.

Use --skip-version-check commandline argument to disable this check.
        """.strip())

    expected_xformers_version = "0.0.17"
    if shared.xformers_available:
        import xformers

        if version.parse(xformers.__version__) < version.parse(expected_xformers_version):
            errors.print_error_explanation(f"""
You are running xformers {xformers.__version__}.
The program is tested to work with xformers {expected_xformers_version}.
To reinstall the desired version, run with commandline flag --reinstall-xformers.

Use --skip-version-check commandline argument to disable this check.
            """.strip())


def restore_config_state_file():
    config_state_file = shared.opts.restore_config_state_file
    if config_state_file == "":
        return

    shared.opts.restore_config_state_file = ""
    shared.opts.save(shared.config_filename)

    if os.path.isfile(config_state_file):
        print(f"*** About to restore extension state from file: {config_state_file}")
        with open(config_state_file, "r", encoding="utf-8") as f:
            config_state = json.load(f)
            config_states.restore_extension_config(config_state)
        startup_timer.record("restore extension config")
    elif config_state_file:
        print(f"!!! Config state backup not found: {config_state_file}")


def validate_tls_options():
    if not (cmd_opts.tls_keyfile and cmd_opts.tls_certfile):
        return

    try:
        if not os.path.exists(cmd_opts.tls_keyfile):
            print("Invalid path to TLS keyfile given")
        if not os.path.exists(cmd_opts.tls_certfile):
            print(f"Invalid path to TLS certfile: '{cmd_opts.tls_certfile}'")
    except TypeError:
        cmd_opts.tls_keyfile = cmd_opts.tls_certfile = None
        print("TLS setup invalid, running webui without TLS")
    else:
        print("Running with TLS")
    startup_timer.record("TLS")


def get_gradio_auth_creds() -> Iterable[tuple[str, ...]]:
    """
    Convert the gradio_auth and gradio_auth_path commandline arguments into
    an iterable of (username, password) tuples.
    """
    def process_credential_line(s) -> tuple[str, ...] | None:
        s = s.strip()
        if not s:
            return None
        return tuple(s.split(':', 1))

    if cmd_opts.gradio_auth:
        for cred in cmd_opts.gradio_auth.split(','):
            cred = process_credential_line(cred)
            if cred:
                yield cred

    if cmd_opts.gradio_auth_path:
        with open(cmd_opts.gradio_auth_path, 'r', encoding="utf8") as file:
            for line in file.readlines():
                for cred in line.strip().split(','):
                    cred = process_credential_line(cred)
                    if cred:
                        yield cred


def configure_sigint_handler():
    # make the program just exit at ctrl+c without waiting for anything
    def sigint_handler(sig, frame):
        print(f'Interrupted with signal {sig} in {frame}')
        os._exit(0)

    if not os.environ.get("COVERAGE_RUN"):
        # Don't install the immediate-quit handler when running under coverage,
        # as then the coverage report won't be generated.
        signal.signal(signal.SIGINT, sigint_handler)


def configure_opts_onchange():
    if not cmd_opts.pureui:
        shared.opts.onchange("sd_model_checkpoint", wrap_queued_call(lambda: modules.sd_models.reload_model_weights()), call=False)

    shared.opts.onchange("sd_vae", wrap_queued_call(lambda: modules.sd_vae.reload_vae_weights()), call=False)
    shared.opts.onchange("sd_vae_as_default", wrap_queued_call(lambda: modules.sd_vae.reload_vae_weights()), call=False)
    shared.opts.onchange("temp_dir", ui_tempdir.on_tmpdir_changed)
    shared.opts.onchange("gradio_theme", shared.reload_gradio_theme)
    shared.opts.onchange("cross_attention_optimization", wrap_queued_call(lambda: modules.sd_hijack.model_hijack.redo_hijack(shared.sd_model)), call=False)
    startup_timer.record("opts onchange")

def initialize():
    fix_asyncio_event_loop_policy()
    validate_tls_options()
    configure_sigint_handler()
    check_versions()
    modelloader.cleanup_models()
    configure_opts_onchange()

    modules.sd_models.setup_model()
    startup_timer.record("setup SD model")

    codeformer.setup_model(cmd_opts.codeformer_models_path)
    startup_timer.record("setup codeformer")

    gfpgan.setup_model(cmd_opts.gfpgan_models_path)
    startup_timer.record("setup gfpgan")

    initialize_rest(reload_script_modules=False)


def initialize_rest(*, reload_script_modules=False):
    """
    Called both from initialize() and when reloading the webui.
    """
    sd_samplers.set_samplers()
    extensions.list_extensions()
    startup_timer.record("list extensions")

    restore_config_state_file()

    if cmd_opts.ui_debug_mode:
        shared.sd_upscalers = upscaler.UpscalerLanczos().scalers
        modules.scripts.load_scripts()
        return

    modules.sd_models.list_models()
    startup_timer.record("list SD models")

    localization.list_localizations(cmd_opts.localizations_dir)

    modules.scripts.load_scripts()
    startup_timer.record("load scripts")

    if reload_script_modules:
        for module in [module for name, module in sys.modules.items() if name.startswith("modules.ui")]:
            importlib.reload(module)
        startup_timer.record("reload script modules")

    modelloader.load_upscalers()
    startup_timer.record("load upscalers")

    modules.sd_vae.refresh_vae_list()
    startup_timer.record("refresh VAE")
    modules.textual_inversion.textual_inversion.list_textual_inversion_templates()
    startup_timer.record("refresh textual inversion templates")

    modules.script_callbacks.on_list_optimizers(modules.sd_hijack_optimizations.list_optimizers)
    modules.sd_hijack.list_optimizers()
    startup_timer.record("scripts list_optimizers")

    def load_model():
        """
        Accesses shared.sd_model property to load model.
        After it's available, if it has been loaded before this access by some extension,
        its optimization may be None because the list of optimizaers has neet been filled
        by that time, so we apply optimization again.
        """

        shared.sd_model  # noqa: B018

        if modules.sd_hijack.current_optimizer is None:
            modules.sd_hijack.apply_optimizations()

    if not cmd_opts.pureui:
        Thread(target=load_model).start()

    shared.reload_hypernetworks()
    startup_timer.record("reload hypernetworks")

    ui_extra_networks.initialize()
    ui_extra_networks.register_default_pages()

    extra_networks.initialize()
    extra_networks.register_default_extra_networks()
    startup_timer.record("initialize extra networks")


def setup_middleware(app):
    app.middleware_stack = None  # reset current middleware to allow modifying user provided list
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    configure_cors_middleware(app)
    app.build_middleware_stack()  # rebuild middleware stack on-the-fly


def configure_cors_middleware(app):
    cors_options = {
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "allow_credentials": True,
    }
    if cmd_opts.cors_allow_origins:
        cors_options["allow_origins"] = cmd_opts.cors_allow_origins.split(',')
    if cmd_opts.cors_allow_origins_regex:
        cors_options["allow_origin_regex"] = cmd_opts.cors_allow_origins_regex
    app.add_middleware(CORSMiddleware, **cors_options)


def create_api(app):
    from modules.api.api import Api
    api = Api(app, queue_lock)
    return api


def api_only():
    initialize()

    app = FastAPI()
    setup_middleware(app)
    api = create_api(app)

    modules.script_callbacks.app_started_callback(None, app)

    print(f"Startup time: {startup_timer.summary()}.")
    api.launch(server_name="0.0.0.0" if cmd_opts.listen else "127.0.0.1", port=cmd_opts.port if cmd_opts.port else 7861)


def stop_route(request):
    shared.state.server_command = "stop"
    return Response("Stopping.")

def user_auth(username, password):
    inputs = {
        'username': username,
        'password': password
    }
    api_endpoint = os.environ['api_endpoint']
    response = requests.post(url=f'{api_endpoint}/sd/login', json=inputs)
    return response.status_code == 200

def get_bucket_and_key(s3uri):
    pos = s3uri.find('/', 5)
    bucket = s3uri[5 : pos]
    key = s3uri[pos + 1 : ]
    return bucket, key

def get_models(path, extensions):
    candidates = []
    models = []
    for extension in extensions:
        candidates = candidates + glob.glob(os.path.join(path, f'**/{extension}'), recursive=True)

    for filename in sorted(candidates, key=str.lower):
        if os.path.isdir(filename):
            continue
        models.append(filename)
    return models

def check_space_s3_download(s3_client,bucket_name,s3_folder,local_folder,file,size,mode):
    print(f"bucket_name:{bucket_name},s3_folder:{s3_folder},file:{file}")
    if file == '' or None:
        print('Debug log:file is empty, return')
        return True
    src = s3_folder + '/' + file
    dist =  os.path.join(local_folder, file)
    os.makedirs(os.path.dirname(dist), exist_ok=True)
    disk_usage = psutil.disk_usage('/tmp')
    freespace = disk_usage.free/(1024**3)
    print(f"Total space: {disk_usage.total/(1024**3)}, Used space: {disk_usage.used/(1024**3)}, Free space: {freespace}")
    if freespace - size >= FREESPACE:
        try:
            s3_client.download_file(bucket_name, src, dist)
            #init ref cnt to 0, when the model file first time download
            hash = modules.sd_models.model_hash(dist)
            if mode == 'sd' :
                shared.sd_models_Ref.add_models_ref('{0} [{1}]'.format(file, hash))
            elif mode == 'cn':
                shared.cn_models_Ref.add_models_ref('{0} [{1}]'.format(os.path.splitext(file)[0], hash))
            elif mode == 'lora':
                shared.lora_models_Ref.add_models_ref('{0} [{1}]'.format(os.path.splitext(file)[0], hash))
            elif mode == 'vae':
                shared.vae_models_Ref.add_models_ref('{0} [{1}]'.format(os.path.splitext(file)[0], hash))
            print(f'download_file success:from {bucket_name}/{src} to {dist}')
        except Exception as e:
            print(f'download_file error: from {bucket_name}/{src} to {dist}')
            print(f"An error occurred: {e}") 
            return False
        return True
    else:
        return False

def free_local_disk(local_folder,size,mode):
    disk_usage = psutil.disk_usage('/tmp')
    freespace = disk_usage.free/(1024**3)
    if freespace - size >= FREESPACE:
        return
    models_Ref = None
    if mode == 'sd' :
        models_Ref = shared.sd_models_Ref
    elif mode == 'cn':
        models_Ref = shared.cn_models_Ref
    elif mode == 'lora':
        models_Ref = shared.lora_models_Ref
    elif mode == 'vae':
        models_Ref = shared.vae_models_Ref
    model_name,ref_cnt  = models_Ref.get_least_ref_model()
    print (f'shared.{mode}_models_Ref:{models_Ref.get_models_ref_dict()} -- model_name:{model_name}')
    if model_name and ref_cnt:
        filename = model_name[:model_name.rfind("[")]
        os.remove(os.path.join(local_folder, filename))
        disk_usage = psutil.disk_usage('/tmp')
        freespace = disk_usage.free/(1024**3)
        print(f"Remove file: {os.path.join(local_folder, filename)} now left space:{freespace}") 
        de_register_model(filename,mode)
    else:
        zero_ref_models = set([model[:model.rfind(" [")] for model, count in models_Ref.get_models_ref_dict().items() if count == 0])
        local_files = set(os.listdir(local_folder))
        files = [(os.path.join(local_folder, file), os.path.getctime(os.path.join(local_folder, file))) for file in zero_ref_models.intersection(local_files)]
        if len(files) == 0:
            print(f"No files to remove in folder: {local_folder}, please remove some files in S3 bucket") 
            return
        files.sort(key=lambda x: x[1])
        oldest_file = files[0][0]
        os.remove(oldest_file)
        disk_usage = psutil.disk_usage('/tmp')
        freespace = disk_usage.free/(1024**3)
        print(f"Remove file: {oldest_file} now left space:{freespace}") 
        filename = os.path.basename(oldest_file)
        de_register_model(filename,mode)

def list_s3_objects(s3_client,bucket_name, prefix=''):
    objects = []
    paginator = s3_client.get_paginator('list_objects_v2')
    page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)
    for page in page_iterator:
        if 'Contents' in page:
            for obj in page['Contents']:
                _, ext = os.path.splitext(obj['Key'].lstrip('/'))
                if ext in ['.pt', '.pth', '.ckpt', '.safetensors','.yaml']:
                    objects.append(obj)
        if 'NextContinuationToken' in page:
            page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix,
                                                ContinuationToken=page['NextContinuationToken'])
    return objects

def initial_s3_download(s3_folder, local_folder,cache_dir,mode):
    os.makedirs(os.path.dirname(local_folder), exist_ok=True)
    os.makedirs(os.path.dirname(cache_dir), exist_ok=True)
    print(f'create dir: {os.path.dirname(local_folder)}')
    print(f'create dir: {os.path.dirname(cache_dir)}')
    s3_file_name = os.path.join(cache_dir,f's3_files_{mode}.json')
    if os.path.isfile(s3_file_name) == False:
        s3_files = {}
        with open(s3_file_name, "w") as f:
            json.dump(s3_files, f)
    s3 = boto3.client('s3')
    s3_objects = list_s3_objects(s3_client=s3, bucket_name=shared.models_s3_bucket, prefix=s3_folder)
    fnames_dict = {}
    for obj in s3_objects:
        filename = obj['Key'].replace(s3_folder, '').lstrip('/')
        root, ext = os.path.splitext(filename)
        model = fnames_dict.get(root)
        if model:
            model.append(filename)
        else:
            fnames_dict[root] = [filename]
    tmp_s3_files = {}
    for obj in s3_objects:
        etag = obj['ETag'].strip('"').strip("'")   
        size = obj['Size']/(1024**3)
        filename = obj['Key'].replace(s3_folder, '').lstrip('/')
        tmp_s3_files[filename] = [etag,size]
    
    if mode == 'sd':
        s3_files = {}
        try:
            _, file_names =  next(iter(fnames_dict.items()))
            for fname in file_names:
                s3_files[fname] = tmp_s3_files.get(fname)
                check_space_s3_download(s3,shared.models_s3_bucket, s3_folder,local_folder, fname, tmp_s3_files.get(fname)[1], mode)
                register_models(local_folder,mode)
        except Exception as e:
            traceback.print_stack()
            print(e)

    print(f'-----s3_files---{s3_files}')
    with open(s3_file_name, "w") as f:
        json.dump(s3_files, f)

def sync_s3_folder(local_folder,cache_dir,mode):
    s3 = boto3.client('s3')
    def sync(mode):
        if mode == 'sd':
            s3_folder = shared.s3_folder_sd 
        elif mode == 'cn':
            s3_folder = shared.s3_folder_cn 
        elif mode == 'lora':
            s3_folder = shared.s3_folder_lora
        elif mode == 'vae':
            s3_folder = shared.s3_folder_vae
        else: 
            s3_folder = ''
        os.makedirs(os.path.dirname(local_folder), exist_ok=True)
        os.makedirs(os.path.dirname(cache_dir), exist_ok=True)
        s3_file_name = os.path.join(cache_dir,f's3_files_{mode}.json')
        if os.path.isfile(s3_file_name) == False:
            s3_files = {}
            with open(s3_file_name, "w") as f:
                json.dump(s3_files, f)

        s3_objects = list_s3_objects(s3_client=s3,bucket_name=shared.models_s3_bucket, prefix=s3_folder)
        s3_files = {}
        for obj in s3_objects:
            etag = obj['ETag'].strip('"').strip("'")   
            size = obj['Size']/(1024**3)
            key = obj['Key'].replace(s3_folder, '').lstrip('/')
            s3_files[key] = [etag,size]

        s3_files_local = {}
        with open(s3_file_name, "r") as f:
            s3_files_local = json.load(f)
        with open(s3_file_name, "w") as f:
            json.dump(s3_files, f)
        mod_files = set()
        new_files = set([key for key in s3_files if key not in s3_files_local])
        del_files = set([key for key in s3_files_local if key not in s3_files])
        registerflag = False
        for key in set(s3_files_local.keys()).intersection(s3_files.keys()):
            local_etag  = s3_files_local.get(key)[0]
            if local_etag and local_etag != s3_files[key][0]:
                mod_files.add(key)
        for file in del_files:
            if os.path.isfile(os.path.join(local_folder, file)):
                os.remove(os.path.join(local_folder, file))
                print(f'remove file {os.path.join(local_folder, file)}')
                de_register_model(file,mode)
        for file in new_files.union(mod_files):
            registerflag = True
            retry = 3 ##retry limit times to prevent dead loop in case other folders is empty
            while retry:
                ret = check_space_s3_download(s3,shared.models_s3_bucket, s3_folder,local_folder, file, s3_files[file][1], mode)
                if ret:
                    retry = 0
                else:
                    free_local_disk(local_folder,s3_files[file][1],mode)
                    retry = retry - 1
        if registerflag:
            register_models(local_folder,mode)
            if mode == 'sd':
                modules.sd_models.list_models()
            elif mode == 'cn':
                modules.script_callbacks.update_cn_models_callback()
            elif mode == 'lora':
                print('update lora')
            elif mode == 'vae':
                modules.sd_vae.refresh_vae_list()

    def sync_thread(mode):  
        while True:
            syncLock.acquire()
            sync(mode)
            syncLock.release()
            time.sleep(30)
    thread = Thread(target=sync_thread,args=(mode,))
    thread.start()
    print (f'{mode}_sync thread start')
    return thread

def register_models(models_dir,mode):
    if mode == 'sd':
        register_sd_models(models_dir)
    elif mode == 'cn':
        register_cn_models(models_dir)
    elif mode == 'lora':
        register_lora_models(models_dir)
    elif mode == 'vae':
        register_vae_models(models_dir)

def register_vae_models(vae_models_dir):
    print ('---register_vae_models()- to be impletemented---')
    if 'endpoint_name' in os.environ:
        items = []
        params = {
            'module': 'VAE'
        }
        api_endpoint = os.environ['api_endpoint']
        endpoint_name = os.environ['endpoint_name']
        for filename in get_models(vae_models_dir, ['*.pt', '*.ckpt', '*.safetensors']):
            item = {}
            item['model_name'] = os.path.basename(filename)
            item['path'] = filename
            item['endpoint_name'] = endpoint_name
            items.append(item)
        inputs = {
            'items': items
        }
        if api_endpoint.startswith('http://') or api_endpoint.startswith('https://'):
            response = requests.post(url=f'{api_endpoint}/sd/models', json=inputs, params=params)
            print(response)

def register_lora_models(lora_models_dir):
    print ('---register_lora_models()----')
    if 'endpoint_name' in os.environ:
        items = []
        params = {
            'module': 'Lora'
        }
        api_endpoint = os.environ['api_endpoint']
        endpoint_name = os.environ['endpoint_name']
        for filename in get_models(lora_models_dir, ['*.pt', '*.ckpt', '*.safetensors']):
            shorthash = modules.hashes.calculate_sha256(os.path.join(lora_models_dir, filename))
            hash = modules.sd_models.model_hash()
            metadata = {}

            is_safetensors = os.path.splitext(filename)[1].lower() == ".safetensors"

            if is_safetensors:
                try:
                    metadata = modules.sd_models.read_metadata_from_safetensors(filename)
                except Exception as e:
                    errors.display(e, f"reading lora {filename}")

            item = {}
            item['model_name'] = os.path.splitext(os.path.basename(filename))[0]
            item['filename'] = os.path.basename(filename)
            item['hash'] = hash
            item['shorthash'] = shorthash
            item['metadata'] = json.dumps(metadata)
            item['endpoint_name'] = endpoint_name
            items.append(item)
        inputs = {
            'items': items
        }
        if api_endpoint.startswith('http://') or api_endpoint.startswith('https://'):
            response = requests.post(url=f'{api_endpoint}/sd/models', json=inputs, params=params)
            print(response)

def register_sd_models(sd_models_dir):
    print ('---register_sd_models()----')
    model_dir = "Stable-diffusion"
    model_path = os.path.abspath(os.path.join(paths.models_path, model_dir))
    
    if 'endpoint_name' in os.environ:
        items = []
        api_endpoint = os.environ['api_endpoint']
        endpoint_name = os.environ['endpoint_name']
        for filename in get_models(sd_models_dir, ['*.ckpt', '*.safetensors']):
            abspath = os.path.abspath(filename)
            if shared.cmd_opts.ckpt_dir is not None and abspath.startswith(shared.cmd_opts.ckpt_dir):
                name = abspath.replace(shared.cmd_opts.ckpt_dir, '')
            elif abspath.startswith(model_path):
                name = abspath.replace(model_path, '')
            else:
                name = os.path.basename(filename)

        if name.startswith("\\") or name.startswith("/"):
            name = name[1:]

            item = {}
            item['name'] = name
            item['name_for_extra'] = name_for_extra = os.path.splitext(os.path.basename(filename))[0]
            item['model_name'] = model_name = os.path.splitext(name.replace("/", "_").replace("\\", "_"))[0]
            item['hash'] = hash = modules.sd_models.model_hash(filename)
            item['sha256'] = sha256 = modules.hashes.sha256_from_cache(filename, f"checkpoint/{name}")
            item['shorthash'] = shorthash = sha256[0:10] if sha256 else None
            item['title'] = title = name if shorthash is None else f'{name} [{shorthash}]'
            item['ids'] = [hash, model_name, title, name, f'{name} [{hash}]'] + ([shorthash, sha256, f'{name} [{shorthash}]'] if shorthash else [])
            item['metadata'] = {}
            _, ext = os.path.splitext(filename)
            if ext.lower() == ".safetensors":
                try:
                    item['metadata'] = modules.sd_models.read_metadata_from_safetensors(filename)
                except Exception as e:
                    errors.display(e, f"reading checkpoint metadata: {filename}")
            item['endpoint_name'] = endpoint_name
            items.append(item)
        inputs = {
            'items': items
        }
        params = {
            'module': 'Stable-diffusion'
        }
        if api_endpoint.startswith('http://') or api_endpoint.startswith('https://'):
            response = requests.post(url=f'{api_endpoint}/sd/models', json=inputs, params=params)
            print(response)

def register_cn_models(cn_models_dir):
    print ('---register_cn_models()----')
    if 'endpoint_name' in os.environ:
        items = []
        api_endpoint = os.environ['api_endpoint']
        endpoint_name = os.environ['endpoint_name']
        params = {
            'module': 'ControlNet'
        }
        for filename in get_models(cn_models_dir, ['*.pt', '*.pth', '*.ckpt', '*.safetensors']):
            hash = modules.sd_models.model_hash(os.path.join(cn_models_dir, filename))
            item = {}
            item['model_name'] = os.path.basename(filename)
            item['title'] = '{0} [{1}]'.format(os.path.splitext(os.path.basename(filename))[0], hash)
            item['endpoint_name'] = endpoint_name
            items.append(item)
        inputs = {
            'items': items
        }
        if api_endpoint.startswith('http://') or api_endpoint.startswith('https://'):
            response = requests.post(url=f'{api_endpoint}/sd/models', json=inputs, params=params)
            print(response)

def sync_images_from_s3():
    # Create a thread function to keep syncing with the S3 folder
    bucket_name = get_default_sagemaker_bucket().replace('s3://','')
    def sync_thread(bucket_name):  
        while True:
            sync_images_lock.acquire()
            shared.download_images_for_ui(bucket_name)
            sync_images_lock.release()
            time.sleep(10)
    thread = Thread(target=sync_thread,args=(bucket_name,))
    thread.start()
    print (f'{bucket_name} images sync thread start ')

def webui():
    launch_api = cmd_opts.api

    if launch_api:
        models_config_s3uri = os.environ.get('models_config_s3uri', None)
        if models_config_s3uri:
            bucket, key = get_bucket_and_key(models_config_s3uri)
            s3_object = shared.s3_client.get_object(Bucket=bucket, Key=key)
            bytes = s3_object["Body"].read()
            payload = bytes.decode('utf8')
            huggingface_models = json.loads(payload).get('huggingface_models', None)
            s3_models = json.loads(payload).get('s3_models', None)
            http_models = json.loads(payload).get('http_models', None)
        else:
            huggingface_models = os.environ.get('huggingface_models', None)
            huggingface_models = json.loads(huggingface_models) if huggingface_models else None
            s3_models = os.environ.get('s3_models', None)
            s3_models = json.loads(s3_models) if s3_models else None
            http_models = os.environ.get('http_models', None)
            http_models = json.loads(http_models) if http_models else None

        if huggingface_models:
            for huggingface_model in huggingface_models:
                repo_id = huggingface_model['repo_id']
                filename = huggingface_model['filename']
                name = huggingface_model['name']

                hf_hub_download(
                    repo_id=repo_id,
                    filename=filename,
                    local_dir=f'/tmp/models/{name}',
                    cache_dir='/tmp/cache/huggingface'
                )

        if s3_models:
            for s3_model in s3_models:
                uri = s3_model['uri']
                name = s3_model['name']
                shared.s3_download(uri, f'/tmp/models/{name}')

        if http_models:
            for http_model in http_models:
                uri = http_model['uri']
                filename = http_model['filename']
                name = http_model['name']
                shared.http_download(uri, f'/tmp/models/{name}/{filename}')

    if not cmd_opts.pureui and not cmd_opts.train:
        print(os.system('df -h'))
        sd_models_tmp_dir = f"{shared.tmp_models_dir}/Stable-diffusion/"
        cn_models_tmp_dir = f"{shared.tmp_models_dir}/ControlNet/"
        lora_models_tmp_dir = f"{shared.tmp_models_dir}/Lora/"
        vae_models_tmp_dir = f"{shared.tmp_models_dir}/VAE/"
        cache_dir = f"{shared.tmp_cache_dir}/"
        sg_s3_bucket = shared.get_default_sagemaker_bucket()
        if not shared.models_s3_bucket:
            shared.models_s3_bucket = os.environ['sg_default_bucket'] if os.environ.get('sg_default_bucket') else sg_s3_bucket
            shared.s3_folder_sd = "stable-diffusion-webui/models/Stable-diffusion"
            shared.s3_folder_cn = "stable-diffusion-webui/models/ControlNet"
            shared.s3_folder_lora = "stable-diffusion-webui/models/Lora"
            shared.s3_folder_vae = "stable-diffusion-webui/models/VAE"


        #only download the cn models and the first sd model from default bucket, to accerlate the startup time
        initial_s3_download(shared.s3_folder_sd,sd_models_tmp_dir,cache_dir,'sd')
        sync_s3_folder(vae_models_tmp_dir,cache_dir,'vae')
        sync_s3_folder(sd_models_tmp_dir,cache_dir,'sd')
        sync_s3_folder(cn_models_tmp_dir,cache_dir,'cn')
        sync_s3_folder(lora_models_tmp_dir,cache_dir,'lora')

    initialize()

    while 1:
        if shared.opts.clean_temp_dir_at_start:
            ui_tempdir.cleanup_tmpdr()
            startup_timer.record("cleanup temp dir")

        modules.script_callbacks.before_ui_callback()
        startup_timer.record("scripts before_ui_callback")

        shared.demo = modules.ui.create_ui()
        startup_timer.record("create ui")

        if not cmd_opts.no_gradio_queue:
            shared.demo.queue(64)

        gradio_auth_creds = list(get_gradio_auth_creds()) or None

        # this restores the missing /docs endpoint
        if launch_api and not hasattr(FastAPI, 'original_setup'):
            # TODO: replace this with `launch(app_kwargs=...)` if https://github.com/gradio-app/gradio/pull/4282 gets merged
            def fastapi_setup(self):
                self.docs_url = "/docs"
                self.redoc_url = "/redoc"
                self.original_setup()

            FastAPI.original_setup = FastAPI.setup
            FastAPI.setup = fastapi_setup

        if cmd_opts.pureui:
            sync_images_from_s3()

        app, local_url, share_url = shared.demo.launch(
            share=cmd_opts.share,
            server_name=server_name,
            server_port=cmd_opts.port,
            ssl_keyfile=cmd_opts.tls_keyfile,
            ssl_certfile=cmd_opts.tls_certfile,
            ssl_verify=cmd_opts.disable_tls_verify,
            debug=cmd_opts.gradio_debug,
            auth=user_auth,
            inbrowser=cmd_opts.autolaunch,
            prevent_thread_lock=True,
            allowed_paths=cmd_opts.gradio_allowed_path,
        )
        if cmd_opts.add_stop_route:
            app.add_route("/_stop", stop_route, methods=["POST"])

        # after initial launch, disable --autolaunch for subsequent restarts
        cmd_opts.autolaunch = False

        startup_timer.record("gradio launch")

        # gradio uses a very open CORS policy via app.user_middleware, which makes it possible for
        # an attacker to trick the user into opening a malicious HTML page, which makes a request to the
        # running web ui and do whatever the attacker wants, including installing an extension and
        # running its code. We disable this here. Suggested by RyotaK.
        app.user_middleware = [x for x in app.user_middleware if x.cls.__name__ != 'CORSMiddleware']

        setup_middleware(app)

        modules.progress.setup_progress_api(app)
        modules.ui.setup_ui_api(app)

        if launch_api:
            create_api(app)

        ui_extra_networks.add_pages_to_demo(app)

        modules.script_callbacks.app_started_callback(shared.demo, app)
        startup_timer.record("scripts app_started_callback")

        print(f"Startup time: {startup_timer.summary()}.")

        if cmd_opts.subpath:
            redirector = FastAPI()
            redirector.get("/")
            gradio.mount_gradio_app(redirector, shared.demo, path=f"/{cmd_opts.subpath}")

        try:
            while True:
                server_command = shared.state.wait_for_server_command(timeout=5)
                if server_command:
                    if server_command in ("stop", "restart"):
                        break
                    else:
                        print(f"Unknown server command: {server_command}")
        except KeyboardInterrupt:
            print('Caught KeyboardInterrupt, stopping...')
            server_command = "stop"

        if server_command == "stop":
            print("Stopping server...")
            # If we catch a keyboard interrupt, we want to stop the server and exit.
            shared.demo.close()
            break
        print('Restarting UI...')
        shared.demo.close()
        time.sleep(0.5)
        startup_timer.reset()
        modules.script_callbacks.app_reload_callback()
        startup_timer.record("app reload callback")
        modules.script_callbacks.script_unloaded_callback()
        startup_timer.record("scripts unloaded callback")
        initialize_rest(reload_script_modules=True)

        modules.script_callbacks.on_list_optimizers(modules.sd_hijack_optimizations.list_optimizers)
        modules.sd_hijack.list_optimizers()
        startup_timer.record("scripts list_optimizers")

if cmd_opts.train:
    def train():
        if cmd_opts.model_name != '':
            for huggingface_model in shared.huggingface_models:
                repo_id = huggingface_model['repo_id']
                filename = huggingface_model['filename']
                if filename == cmd_opts.model_name:
                    hf_hub_download(
                        repo_id=repo_id,
                        filename=filename,
                        local_dir='/opt/ml/input/data/models',
                        cache_dir='/opt/ml/input/data/models'
                    )
                    if filename in ['v2-1_768-ema-pruned.ckpt', 'v2-1_768-nonema-pruned.ckpt', '768-v-ema.ckpt', '']:
                        name = os.path.splitext(filename)[0]
                        shared.http_download(
                            'https://raw.githubusercontent.com/Stability-AI/stablediffusion/main/configs/stable-diffusion/v2-inference-v.yaml',
                            f'/opt/ml/input/data/models/{name}.yaml'
                        )

        initialize()

        train_task = cmd_opts.train_task
        train_args = json.loads(cmd_opts.train_args)

        embeddings_s3uri = cmd_opts.embeddings_s3uri
        hypernetwork_s3uri = cmd_opts.hypernetwork_s3uri
        sd_models_s3uri = cmd_opts.sd_models_s3uri
        db_models_s3uri = cmd_opts.db_models_s3uri
        lora_models_s3uri = cmd_opts.lora_models_s3uri
        api_endpoint = cmd_opts.api_endpoint
        username = cmd_opts.username

        if username != '' and train_task in ['embedding', 'hypernetwork']:
            inputs = {
                'action': 'get',
                'username': username
            }
            response = requests.post(url=f'{api_endpoint}/sd/user', json=inputs)
            if response.status_code == 200 and response.text != '':
                data = json.loads(response.text)
                try:
                    opts.data = json.loads(data['options'])
                except Exception as e:
                    print(e)
                modules.sd_models.load_model()

        if train_task == 'embedding':
            name = train_args['embedding_settings']['name']
            nvpt = train_args['embedding_settings']['nvpt']
            overwrite_old = train_args['embedding_settings']['overwrite_old']
            initialization_text = train_args['embedding_settings']['initialization_text']
            modules.textual_inversion.textual_inversion.create_embedding(
                name,
                nvpt,
                overwrite_old,
                init_text=initialization_text
            )
            if not cmd_opts.pureui:
                modules.sd_hijack.model_hijack.embedding_db.load_textual_inversion_embeddings()
            process_src = '/opt/ml/input/data/images'
            process_dst = str(uuid.uuid4())
            process_width = train_args['images_preprocessing_settings']['process_width']
            process_height = train_args['images_preprocessing_settings']['process_height']
            preprocess_txt_action = train_args['images_preprocessing_settings']['preprocess_txt_action']
            process_keep_original_size = train_args['images_preprocessing_settings']['embedding_process_keep_original_size']
            process_flip = train_args['images_preprocessing_settings']['process_flip']
            process_split = train_args['images_preprocessing_settings']['process_split']
            process_caption = train_args['images_preprocessing_settings']['process_caption']    
            process_caption_deepbooru = train_args['images_preprocessing_settings']['process_caption_deepbooru']    
            process_split_threshold = train_args['images_preprocessing_settings']['process_split_threshold']
            process_overlap_ratio = train_args['images_preprocessing_settings']['process_overlap_ratio']
            process_focal_crop = train_args['images_preprocessing_settings']['process_focal_crop']
            process_focal_crop_face_weight = train_args['images_preprocessing_settings']['process_focal_crop_face_weight']    
            process_focal_crop_entropy_weight = train_args['images_preprocessing_settings']['process_focal_crop_entropy_weight']    
            process_focal_crop_edges_weight = train_args['images_preprocessing_settings']['process_focal_crop_debug']
            process_focal_crop_debug = train_args['images_preprocessing_settings']['process_focal_crop_debug']
            process_multicrop_mindim = train_args['images_preprocessing_settings']['process_multicrop_mindim']
            process_multicrop_maxdim = train_args['images_preprocessing_settings']['process_multicrop_maxdim']
            process_multicrop_minarea = train_args['process_multicrop_minarea']['process_multicrop_minarea']
            process_multicrop_maxarea = train_args['process_multicrop_minarea']['process_multicrop_maxarea']
            process_multicrop_objective = train_args['process_multicrop_minarea']['process_multicrop_objective']
            process_multicrop_threshold = train_args['process_multicrop_minarea']['process_multicrop_threshold']
            modules.textual_inversion.preprocess.preprocess(
                None,
                process_src,
                process_dst,
                process_width,
                process_height,
                preprocess_txt_action,
                process_keep_original_size,
                process_flip,
                process_split,
                process_caption,
                process_caption_deepbooru,
                process_split_threshold,
                process_overlap_ratio,
                process_focal_crop,
                process_focal_crop_face_weight,
                process_focal_crop_entropy_weight,
                process_focal_crop_edges_weight,
                process_focal_crop_debug,
                process_multicrop_mindim,
                process_multicrop_maxdim,
                process_multicrop_minarea,
                process_multicrop_maxarea,
                process_multicrop_objective,
                process_multicrop_threshold
            )
            train_embedding_name = name
            learn_rate = train_args['train_embedding_settings']['learn_rate']
            clip_grad_mode = train_args['train_embedding_settings']['clip_grad_mode']
            clip_grad_value = train_args['train_embedding_settings']['clip_grad_value']
            batch_size = train_args['train_embedding_settings']['batch_size']
            gradient_step = train_args['train_embedding_settings']['gradient_step']
            data_root = process_dst
            log_directory = 'textual_inversion'
            training_width = train_args['train_embedding_settings']['training_width']
            training_height = train_args['train_embedding_settings']['training_height']
            varsize = train_args['train_embedding_settings']['varsize']
            steps = train_args['train_embedding_settings']['steps']
            shuffle_tags = train_args['train_embedding_settings']['shuffle_tags']
            tag_drop_out = train_args['train_embedding_settings']['tag_drop_out']
            latent_sampling_method = train_args['train_embedding_settings']['latent_sampling_method']
            use_weight = train_args['train_embedding_settings']['use_weight']
            create_image_every = train_args['train_embedding_settings']['create_image_every']
            save_embedding_every = train_args['train_embedding_settings']['save_embedding_every']
            template_file = os.path.join(script_path, "textual_inversion_templates", "style_filewords.txt")
            save_image_with_stored_embedding = train_args['train_embedding_settings']['save_image_with_stored_embedding']
            preview_from_txt2img = train_args['train_embedding_settings']['preview_from_txt2img']
            txt2img_preview_params = train_args['train_embedding_settings']['txt2img_preview_params']
            _, filename = modules.textual_inversion.textual_inversion.train_embedding(
                None,
                train_embedding_name,
                learn_rate,
                batch_size,
                gradient_step,
                data_root,
                log_directory,
                training_width,
                training_height,
                varsize,
                steps,
                clip_grad_mode,
                clip_grad_value,
                shuffle_tags,
                tag_drop_out,
                latent_sampling_method,
                use_weight,
                create_image_every,
                save_embedding_every,
                template_file,
                save_image_with_stored_embedding,
                preview_from_txt2img,
                *txt2img_preview_params
            )
            try:
                shared.upload_s3files(
                    embeddings_s3uri, 
                    os.path.join(cmd_opts.embeddings_dir, '{0}.pt'.format(train_embedding_name))
                )
            except Exception as e:
                traceback.print_exc()
                print(e)
        elif train_task == 'hypernetwork':
            name = train_args['hypernetwork_settings']['name']
            enable_sizes = train_args['hypernetwork_settings']['enable_sizes']
            overwrite_old = train_args['hypernetwork_settings']['overwrite_old']
            layer_structure = train_args['hypernetwork_settings']['layer_structure'] if 'layer_structure' in train_args['hypernetwork_settings'] else None
            activation_func = train_args['hypernetwork_settings']['activation_func'] if 'activation_func' in train_args['hypernetwork_settings'] else None
            weight_init = train_args['hypernetwork_settings']['weight_init'] if 'weight_init' in train_args['hypernetwork_settings'] else None
            add_layer_norm = train_args['hypernetwork_settings']['add_layer_norm'] if 'add_layer_norm' in train_args['hypernetwork_settings'] else False
            use_dropout = train_args['hypernetwork_settings']['use_dropout'] if 'use_dropout' in train_args['hypernetwork_settings'] else False

            name = "".join( x for x in name if (x.isalnum() or x in "._- "))

            fn = os.path.join(shared.cmd_opts.hypernetwork_dir, f"{name}.pt")
            if not overwrite_old:
                assert not os.path.exists(fn), f"file {fn} already exists"

            if type(layer_structure) == str:
                layer_structure = [float(x.strip()) for x in layer_structure.split(",")]

            if use_dropout and dropout_structure and type(dropout_structure) == str:
                dropout_structure = [float(x.strip()) for x in dropout_structure.split(",")]
            else:
                dropout_structure = [0] * len(layer_structure)

            hypernet = modules.hypernetworks.hypernetwork.Hypernetwork(
                name=name,
                enable_sizes=[int(x) for x in enable_sizes],
                layer_structure=layer_structure,
                activation_func=activation_func,
                weight_init=weight_init,
                add_layer_norm=add_layer_norm,
                use_dropout=use_dropout,
            )
            hypernet.save(fn)

            shared.hypernetworks = modules.hypernetworks.hypernetwork.list_hypernetworks(cmd_opts.hypernetwork_dir)
            
            process_src = '/opt/ml/input/data/images'
            process_dst = str(uuid.uuid4())
            process_width = train_args['images_preprocessing_settings']['process_width']
            process_height = train_args['images_preprocessing_settings']['process_height']
            preprocess_txt_action = train_args['images_preprocessing_settings']['preprocess_txt_action']
            process_keep_original_size = train_args['images_preprocessing_settings']['embedding_process_keep_original_size']
            process_flip = train_args['images_preprocessing_settings']['process_flip']
            process_split = train_args['images_preprocessing_settings']['process_split']
            process_caption = train_args['images_preprocessing_settings']['process_caption']    
            process_caption_deepbooru = train_args['images_preprocessing_settings']['process_caption_deepbooru']    
            process_split_threshold = train_args['images_preprocessing_settings']['process_split_threshold']
            process_overlap_ratio = train_args['images_preprocessing_settings']['process_overlap_ratio']
            process_focal_crop = train_args['images_preprocessing_settings']['process_focal_crop']
            process_focal_crop_face_weight = train_args['images_preprocessing_settings']['process_focal_crop_face_weight']    
            process_focal_crop_entropy_weight = train_args['images_preprocessing_settings']['process_focal_crop_entropy_weight']    
            process_focal_crop_edges_weight = train_args['images_preprocessing_settings']['process_focal_crop_debug']
            process_focal_crop_debug = train_args['images_preprocessing_settings']['process_focal_crop_debug']
            process_multicrop_mindim = train_args['images_preprocessing_settings']['process_multicrop_mindim']
            process_multicrop_maxdim = train_args['images_preprocessing_settings']['process_multicrop_maxdim']
            process_multicrop_minarea = train_args['process_multicrop_minarea']['process_multicrop_minarea']
            process_multicrop_maxarea = train_args['process_multicrop_minarea']['process_multicrop_maxarea']
            process_multicrop_objective = train_args['process_multicrop_minarea']['process_multicrop_objective']
            process_multicrop_threshold = train_args['process_multicrop_minarea']['process_multicrop_threshold']
            modules.textual_inversion.preprocess.preprocess(
                process_src,
                process_dst,
                process_width,
                process_height,
                preprocess_txt_action,
                process_keep_original_size,
                process_flip,
                process_split,
                process_caption,
                process_caption_deepbooru,
                process_split_threshold,
                process_overlap_ratio,
                process_focal_crop,
                process_focal_crop_face_weight,
                process_focal_crop_entropy_weight,
                process_focal_crop_edges_weight,
                process_focal_crop_debug,
                process_multicrop_mindim,
                process_multicrop_maxdim,
                process_multicrop_minarea,
                process_multicrop_maxarea,
                process_multicrop_objective,
                process_multicrop_threshold
            )
            train_hypernetwork_name = name
            learn_rate = train_args['train_hypernetwork_settings']['learn_rate']
            clip_grad_mode = train_args['train_hypernetwork_settings']['clip_grad_mode']
            clip_grad_value = train_args['train_hypernetwork_settings']['clip_grad_value']
            batch_size = train_args['train_hypernetwork_settings']['batch_size']
            gradient_step = train_args['train_hypernetwork_settings']['gradient_step']
            dataset_directory = process_dst
            log_directory = 'textual_inversion'
            training_width = train_args['train_hypernetwork_settings']['training_width']
            training_height = train_args['train_hypernetwork_settings']['training_height']
            varsize = train_args['train_hypernetwork_settings']['varsize']
            steps = train_args['train_hypernetwork_settings']['steps']
            shuffle_tags = train_args['train_hypernetwork_settings']['shuffle_tags']
            tag_drop_out = train_args['train_hypernetwork_settings']['tag_drop_out']
            latent_sampling_method = train_args['train_hypernetwork_settings']['latent_sampling_method']
            use_weight = train_args['train_embedding_settings']['use_weight']
            create_image_every = train_args['train_hypernetwork_settings']['create_image_every']
            save_hypernetwork_every = train_args['train_hypernetwork_settings']['save_embedding_every']
            template_file = os.path.join(script_path, "textual_inversion_templates", "style_filewords.txt")
            save_image_with_stored_embedding = train_args['train_hypernetwork_settings']['save_image_with_stored_embedding']
            preview_from_txt2img = train_args['train_hypernetwork_settings']['preview_from_txt2img']
            txt2img_preview_params = train_args['train_hypernetwork_settings']['txt2img_preview_params']        
            _, filename = modules.hypernetworks.hypernetwork.train_hypernetwork(
                None,
                train_hypernetwork_name,
                learn_rate,
                batch_size,
                gradient_step,
                dataset_directory,
                log_directory,
                training_width,
                training_height,
                varsize,
                steps,
                clip_grad_mode,
                clip_grad_value,
                shuffle_tags,
                tag_drop_out,
                latent_sampling_method,
                use_weight,
                create_image_every,
                save_hypernetwork_every,
                template_file,
                preview_from_txt2img,
                *txt2img_preview_params
            )
            try:
                shared.upload_s3files(
                    hypernetwork_s3uri, 
                    os.path.join(cmd_opts.hypernetwork_dir, '{0}.pt'.format(train_hypernetwork_name))
                )
            except Exception as e:
                traceback.print_exc()
                print(e)
        elif train_task == 'dreambooth':
            train_dreambooth(api_endpoint, train_args, sd_models_s3uri, db_models_s3uri, lora_models_s3uri, username)
        else:
            print('Incorrect training task')
            exit(-1)

if __name__ == "__main__":
    if cmd_opts.train:
        train()
    elif cmd_opts.nowebui:
        api_only()
    else:
        webui()
