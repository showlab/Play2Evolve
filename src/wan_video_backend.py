"""Optional local Wan2.2 5B / I2V-A14B adapter; no torch imports in the caller.

Select H3_BACKEND=wan. Existing local/MiniMax H3 paths are unchanged.
"""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import warnings
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parent.parent


def _runtime_dir():
    directory = Path(tempfile.gettempdir()) / f'gamecraft-wan-{os.getuid()}'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise PermissionError(f'Wan runtime directory must be private: {directory}')
    return directory


def _runtime_paths(device):
    ident = hashlib.sha256(device.encode()).hexdigest()[:16]
    directory = _runtime_dir()
    return directory / f'{ident}.lock', directory / f'{ident}.sock', directory / f'{ident}.log'


def _server_signature(cfg):
    # A server with different weights/runtime/VRAM policy must not answer this request.
    values = {**cfg, 'extra_vram_gib': os.getenv('WAN_MAX_EXTRA_VRAM_GIB', '20'),
              'backend_mtime_ns': Path(__file__).stat().st_mtime_ns}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _exchange(sock_path, message, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(max(.01, timeout))
        connection.connect(str(sock_path))
        connection.sendall(json.dumps(message).encode() + b'\n')
        with connection.makefile('rb') as stream:
            line = stream.readline()
    if not line:
        raise ConnectionError('Wan server closed the connection without a reply')
    return json.loads(line)


def _ensure_server(cfg, sock_path, server_log, deadline):
    if sock_path.exists():
        try:
            response = _exchange(sock_path, {'op': 'health'}, 1)
            if response.get('signature') != _server_signature(cfg):
                raise RuntimeError('Wan server configuration changed; stop the old server before using new weights or VRAM settings')
            return
        except (ConnectionError, OSError, TimeoutError):
            sock_path.unlink(missing_ok=True)
    env = dict(os.environ, CUDA_VISIBLE_DEVICES=cfg['device'],
               WAN_MODEL_VARIANT=cfg.get('variant', 'ti2v_5b'), WAN_MODEL_PATH=cfg['weights'],
               WAN_CUDA_VISIBLE_DEVICES=cfg['device'], PYTHONUNBUFFERED='1',
               TOKENIZERS_PARALLELISM='false', DIFFSYNTH_SKIP_DOWNLOAD='True')
    with server_log.open('a') as log:
        process = subprocess.Popen([cfg['python'], str(Path(__file__).resolve()), '--server', str(sock_path)],
                                   env=env, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'Wan server exited during startup; see {server_log}')
        try:
            response = _exchange(sock_path, {'op': 'health'}, 1)
            if response.get('signature') == _server_signature(cfg):
                return
        except (ConnectionError, OSError, TimeoutError):
            time.sleep(.1)
    raise TimeoutError(f'Timed out starting Wan server; see {server_log}')


def stop_server():
    """Release this device's persistent Wan model after the current request."""
    cfg = settings()
    lock_path, sock_path, _ = _runtime_paths(cfg['device'])
    if not sock_path.exists():
        return False
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            response = _exchange(sock_path, {'op': 'stop'}, 10)
        except (ConnectionError, OSError, TimeoutError):
            sock_path.unlink(missing_ok=True)
            return False
    return response.get('status') == 'stopped'


def warmup_server(timeout=600):
    """Load the configured model once, then leave it resident for later requests."""
    cfg = preflight()
    lock_path, sock_path, server_log = _runtime_paths(cfg['device'])
    deadline = time.monotonic() + timeout
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _ensure_server(cfg, sock_path, server_log, deadline)
        response = _exchange(sock_path, {'op': 'warmup', 'signature': _server_signature(cfg)},
                             deadline-time.monotonic())
    if response.get('status') != 'ready' or not response.get('model_loaded'):
        raise RuntimeError(f'Wan warmup failed: {response}')
    return response


def server_health():
    cfg = settings()
    _, sock_path, _ = _runtime_paths(cfg['device'])
    if not sock_path.exists():
        return {'status': 'not_running'}
    response = _exchange(sock_path, {'op': 'health'}, 5)
    response['configuration_matches'] = response.get('signature') == _server_signature(cfg)
    return response


def settings(variant_override=None):
    selected_variant = os.getenv('WAN_MODEL_VARIANT', 'ti2v_5b')
    variant = variant_override or selected_variant
    defaults = {
        'ti2v_5b': str(Path.home()/'.cache/play2evolve/models/Wan2.2-TI2V-5B'),
        'i2v_a14b_distill4': str(Path.home()/'.cache/play2evolve/models/Wan2.2-I2V-A14B-distill4step-native'),
    }
    if variant not in defaults:
        raise ValueError(f'Unknown WAN_MODEL_VARIANT: {variant}')
    if variant_override and variant_override != selected_variant:
        if selected_variant != 'i2v_a14b_distill4' or variant != 'ti2v_5b':
            raise ValueError('Only a TI2V-5B fallback for I2V-A14B is supported')
        device = os.getenv('WAN_T2V_FALLBACK_DEVICE')
        if not device or device == os.getenv('WAN_CUDA_VISIBLE_DEVICES', '0'):
            raise ValueError('WAN_T2V_FALLBACK_DEVICE must select another GPU')
        weights = os.getenv('WAN_T2V_MODEL_PATH', defaults[variant])
    else:
        device = os.getenv('WAN_CUDA_VISIBLE_DEVICES', '0')
        weights = os.getenv('WAN_MODEL_PATH', defaults[variant])
    return {
        'python': os.getenv('WAN_PYTHON', sys.executable),
        'weights': weights,
        'source': os.getenv('WAN_DIFFSYNTH_PATH', str(Path.home()/'.cache/play2evolve/DiffSynth-Studio')),
        'device': device,
        'variant': variant,
    }


def preflight(variant_override=None):
    cfg = settings(variant_override)
    required = [Path(cfg['python']), Path(cfg['source'])/'diffsynth/pipelines/wan_video.py',
                Path(cfg['weights'])/'models_t5_umt5-xxl-enc-bf16.pth',
                Path(cfg['weights'])/'google/umt5-xxl/tokenizer_config.json']
    if cfg['variant'] == 'ti2v_5b':
        required += [Path(cfg['weights'])/'Wan2.2_VAE.pth']
        required += [Path(cfg['weights'])/f'diffusion_pytorch_model-0000{i}-of-00003.safetensors' for i in (1,2,3)]
    else:
        required += [Path(cfg['weights'])/'Wan2.1_VAE.pth']
        required += [Path(cfg['weights'])/name/'diffusion_pytorch_model.safetensors'
                     for name in ('high_noise_model', 'low_noise_model')]
    for path in required:
        if not path.is_file():
            raise FileNotFoundError(f'Wan runtime/checkpoint file missing: {path}')
    return cfg


def translate(body, variant='ti2v_5b', steps_override=None):
    task = body.get('task')
    if task not in ('t2va', 'fl2va'):
        raise ValueError(f'Wan does not support task {task!r}')
    prompt = body.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('Wan requires a nonempty prompt')
    # Remove only H3 transport headings/alignment; preserve the scene description.
    if 'integrated_multimodal_description:' in prompt:
        prompt = prompt.split('integrated_multimodal_description:', 1)[1]
    prompt = re.split(r'\n\s*(?:overall_soundscape|non_diegetic_music):', prompt, maxsplit=1)[0]
    prompt = re.sub(r'^\s*\[Shot 1\]\s*', '', prompt).strip()
    if not prompt:
        raise ValueError('Wan visual prompt is empty after removing H3 headings')
    images = {}
    for condition in body.get('conditions') or []:
        index = condition.get('frame_index')
        if condition.get('type') != 'image' or condition.get('role') != 'keyframe' or index not in (0, -1) or index in images:
            raise ValueError('Wan accepts one local first-frame image and optional loop reference')
        path = Path(condition['uri']).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f'Wan reference missing: {path}')
        images[index] = path
    if (task == 't2va' and images) or (task == 'fl2va' and 0 not in images):
        raise ValueError('Wan t2va must have no images; fl2va requires a first frame')
    if variant == 'i2v_a14b_distill4' and 0 not in images:
        raise ValueError('Wan2.2-I2V-A14B requires a first-frame image; use TI2V-5B for text-only assets')
    loop = -1 in images
    if loop:
        if os.getenv('WAN_LOOP_POLICY', 'prompt') not in ('prompt', 'error'):
            raise ValueError('WAN_LOOP_POLICY must be prompt or error')
        if os.getenv('WAN_LOOP_POLICY', 'prompt') == 'error':
            raise ValueError('The configured Wan backend does not enforce last-frame conditioning')
        if images[0].read_bytes() != images[-1].read_bytes():
            raise ValueError('The configured Wan backend cannot honor a distinct final reference image')
        prompt += ' Return to the opening pose and composition for a seamless loop.'
    target = body.get('target') or {}
    seconds = float(body.get('seconds', target.get('duration_seconds', 4)))
    if not math.isfinite(seconds) or not 0 < seconds <= 15:
        raise ValueError('Wan duration must be between 0 and 15 seconds')
    ratio = target.get('aspect_ratio', '16:9')
    try:
        a, b = [float(v) for v in ratio.split(':')]
        if not all(math.isfinite(v) and v > 0 for v in (a,b)):
            raise ValueError()
    except (AttributeError, ValueError):
        raise ValueError('Invalid Wan aspect ratio') from None
    short = int(os.getenv('WAN_SHORT_EDGE', '480'))
    steps = int(steps_override if steps_override is not None else
                os.getenv('WAN_STEPS', '4' if variant == 'i2v_a14b_distill4' else '30'))
    if not 128 <= short <= 768 or not 1 <= steps <= 100:
        raise ValueError('WAN_SHORT_EDGE must be 128..768; WAN_STEPS must be 1..100')
    width = round(short * max(1, a/b) / 16) * 16
    height = round(short * max(1, b/a) / 16) * 16
    if max(width, height) > 2048:
        raise ValueError('Wan requested aspect ratio is too extreme')
    frames = max(5, round((seconds * 24 - 1)/4) * 4 + 1)
    return {'prompt': prompt, 'image': str(images[0]) if images else None,
            'variant': variant,
            'loop_conditioning': 'prompt_only' if loop else 'none',
            'width': width, 'height': height, 'frames': frames, 'fps': 24,
            'steps': steps, 'seed': int(body.get('seed', 0)),
            'requested_seconds': seconds, 'actual_seconds': frames/24,
            'negative_prompt': os.getenv('WAN_NEGATIVE_PROMPT', 'text, watermark, distorted limbs, camera cuts')}


def generate(body, out_mp4, timeout=600):
    fallback = os.getenv('WAN_MODEL_VARIANT') == 'i2v_a14b_distill4' and body.get('task') == 't2va'
    cfg = preflight('ti2v_5b') if fallback else preflight()
    steps_override = int(os.getenv('WAN_T2V_STEPS', '30')) if fallback else None
    request = translate(body, cfg.get('variant', 'ti2v_5b'), steps_override)
    if request['loop_conditioning'] == 'prompt_only':
        warnings.warn('Wan loop uses first-frame + prompt only; final-frame equality is not enforced', RuntimeWarning)
    output = Path(out_mp4).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    request.update(weights=cfg['weights'], source=cfg['source'])
    request_path = output.with_suffix('.wan_request.json')
    request_path.write_text(json.dumps(request, indent=2, ensure_ascii=False))
    lock_path, sock_path, server_log = _runtime_paths(cfg['device'])
    # One request at a time per device, including requests from other callers.
    deadline = time.monotonic() + float(os.getenv('WAN_TIMEOUT', str(timeout)))
    with lock_path.open('a') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Timed out waiting for the Wan GPU queue')
                time.sleep(.5)
        log_path = output.with_suffix('.wan.log')
        if os.getenv('WAN_PERSISTENT', '1') == '0':
            with tempfile.TemporaryDirectory(prefix='wan-', dir=output.parent) as temp:
                generated = Path(temp)/'result.mp4'
                env = dict(os.environ, CUDA_VISIBLE_DEVICES=cfg['device'],
                           WAN_MODEL_VARIANT=cfg.get('variant', 'ti2v_5b'), WAN_MODEL_PATH=cfg['weights'],
                           WAN_CUDA_VISIBLE_DEVICES=cfg['device'], PYTHONUNBUFFERED='1',
                           TOKENIZERS_PARALLELISM='false', DIFFSYNTH_SKIP_DOWNLOAD='True')
                with log_path.open('w') as log:
                    result = subprocess.run([cfg['python'], str(Path(__file__).resolve()), '--worker',
                                             str(request_path), str(generated)], env=env,
                                            stdout=log, stderr=subprocess.STDOUT,
                                            timeout=max(.01, deadline-time.monotonic()))
                if result.returncode or not generated.is_file() or not generated.stat().st_size:
                    tail = log_path.read_text(errors='replace')[-2500:]
                    raise RuntimeError(f'Wan generation failed; see {log_path}\n{tail}')
                os.replace(generated, output)
            return
        if os.getenv('WAN_PERSISTENT', '1') != '1':
            raise ValueError('WAN_PERSISTENT must be 0 or 1')
        _ensure_server(cfg, sock_path, server_log, deadline)
        # A timeout leaves this unique temporary path intact: the server may still
        # be finishing the request, and the previous MP4 must remain untouched.
        generated = output.parent / f'.{output.stem}.wan-{os.getpid()}-{time.time_ns()}.mp4'
        try:
            response = _exchange(sock_path, {'op': 'generate', 'signature': _server_signature(cfg),
                                             'request_path': str(request_path),
                                             'output_path': str(generated), 'log_path': str(log_path)},
                                 deadline-time.monotonic())
            if response.get('status') != 'completed' or not generated.is_file() or not generated.stat().st_size:
                tail = log_path.read_text(errors='replace')[-2500:] if log_path.exists() else ''
                raise RuntimeError(f"Wan generation failed: {response.get('error', 'missing output')}; see {log_path}\n{tail}")
            os.replace(generated, output)
        except TimeoutError:
            raise
        except Exception:
            generated.unlink(missing_ok=True)
            raise


def _load_pipeline(request):
    sys.path.insert(0, request['source'])
    import torch
    import diffsynth.models.wan_video_dit as dit
    from diffsynth.pipelines.wan_video import ModelConfig, WanVideoPipeline
    dit.FLASH_ATTN_2_AVAILABLE = dit.FLASH_ATTN_2_AVAILABLE and hasattr(dit.flash_attn, 'flash_attn_func')
    if not torch.cuda.is_available():
        raise RuntimeError('Wan CUDA unavailable; check WAN_PYTHON and GPU access')
    weights = Path(request['weights'])
    if request.get('variant', 'ti2v_5b') == 'i2v_a14b_distill4':
        free, _ = torch.cuda.mem_get_info()
        if free < 70 * 1024**3:
            raise RuntimeError('Wan I2V-A14B 4-step needs at least 70 GiB free on one GPU')
        vram = dict(offload_dtype=torch.bfloat16, offload_device='cuda',
                    onload_dtype=torch.bfloat16, onload_device='cuda',
                    preparing_dtype=torch.bfloat16, preparing_device='cuda',
                    computation_dtype=torch.bfloat16, computation_device='cuda')
        return WanVideoPipeline.from_pretrained(torch_dtype=torch.bfloat16, device='cuda',
            model_configs=[
                ModelConfig(path=str(weights/name/'diffusion_pytorch_model.safetensors'), **vram)
                for name in ('high_noise_model', 'low_noise_model')
            ] + [ModelConfig(path=str(weights/'models_t5_umt5-xxl-enc-bf16.pth'), **vram),
                 ModelConfig(path=str(weights/'Wan2.1_VAE.pth'), **vram)],
            tokenizer_config=ModelConfig(path=str(weights/'google/umt5-xxl')))
    free, total = torch.cuda.mem_get_info()
    gib = 1024**3
    extra = min(float(os.getenv('WAN_MAX_EXTRA_VRAM_GIB', '20')), free/gib - 12)
    if not math.isfinite(extra) or extra < 8:
        raise RuntimeError('Wan needs at least 20 GiB free, including 12 GiB headroom')
    # DiffSynth's threshold includes other processes; this is not a hard allocator cap.
    limit = (total-free)/gib + extra
    vram = dict(offload_dtype=torch.bfloat16, offload_device='cpu',
                onload_dtype=torch.bfloat16, onload_device='cpu',
                preparing_dtype=torch.bfloat16, preparing_device='cuda',
                computation_dtype=torch.bfloat16, computation_device='cuda')
    return WanVideoPipeline.from_pretrained(torch_dtype=torch.bfloat16, device='cuda', vram_limit=limit,
        model_configs=[
            ModelConfig(path=[str(weights/f'diffusion_pytorch_model-0000{i}-of-00003.safetensors') for i in (1,2,3)], **vram),
            ModelConfig(path=str(weights/'models_t5_umt5-xxl-enc-bf16.pth'), **vram),
            ModelConfig(path=str(weights/'Wan2.2_VAE.pth'), **vram)],
        tokenizer_config=ModelConfig(path=str(weights/'google/umt5-xxl')))


def _render(pipe, request, output):
    from PIL import Image, ImageOps
    from diffsynth.utils.data import save_video
    image = None
    if request['image']:
        with Image.open(request['image']) as source:
            image = ImageOps.pad(source.convert('RGB'), (request['width'], request['height']),
                                 color=source.convert('RGB').getpixel((0,0)))
    kwargs = {}
    if request.get('variant') == 'i2v_a14b_distill4':
        kwargs = {'cfg_scale': 1.0, 'switch_DiT_boundary': 0.9}
    video = pipe(prompt=request['prompt'], negative_prompt=request['negative_prompt'], input_image=image,
                 height=request['height'], width=request['width'], num_frames=request['frames'],
                 num_inference_steps=request['steps'], seed=request['seed'], tiled=True, **kwargs)
    save_video(video, output, fps=request['fps'], quality=5)


def worker(request_path, output):
    request = json.loads(Path(request_path).read_text())
    _render(_load_pipeline(request), request, output)


def serve(sock_path):
    """Keep one model in memory and serialize requests on this device."""
    cfg = preflight()
    signature = _server_signature(cfg)
    sock_path = Path(sock_path)
    sock_path.unlink(missing_ok=True)
    pipe = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(sock_path))
        sock_path.chmod(0o600)
        server.listen(4)
        idle_seconds = float(os.getenv('WAN_IDLE_SECONDS', '3600'))
        if not math.isfinite(idle_seconds) or idle_seconds < 0:
            raise ValueError('WAN_IDLE_SECONDS must be nonnegative and finite')
        server.settimeout(idle_seconds or None)
        try:
            while True:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    return
                with connection:
                    message = {}
                    try:
                        with connection.makefile('rb') as stream:
                            message = json.loads(stream.readline())
                        if message.get('op') == 'health':
                            result = {'status': 'ready', 'signature': signature, 'model_loaded': pipe is not None}
                        elif message.get('op') == 'warmup':
                            if message.get('signature') != signature:
                                raise ValueError('Wan server configuration mismatch')
                            if pipe is None:
                                pipe = _load_pipeline(cfg)
                            result = {'status': 'ready', 'model_loaded': True}
                        elif message.get('op') == 'stop':
                            result = {'status': 'stopped'}
                        elif message.get('op') == 'generate':
                            if message.get('signature') != signature:
                                raise ValueError('Wan server configuration mismatch')
                            request = json.loads(Path(message['request_path']).read_text())
                            log_path = Path(message['log_path'])
                            with log_path.open('w') as log:
                                with redirect_stdout(log), redirect_stderr(log):
                                    if pipe is None:
                                        pipe = _load_pipeline(request)
                                    _render(pipe, request, message['output_path'])
                            result = {'status': 'completed'}
                        else:
                            raise ValueError('Unknown Wan server operation')
                    except Exception as exc:
                        result = {'status': 'failed', 'error': repr(exc)}
                    try:
                        connection.sendall(json.dumps(result).encode() + b'\n')
                    except BrokenPipeError:
                        pass
                    if message.get('op') == 'stop':
                        return
        finally:
            sock_path.unlink(missing_ok=True)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        worker(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] == '--server':
        serve(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == '--stop-server':
        print('stopped' if stop_server() else 'not running')
    elif len(sys.argv) == 2 and sys.argv[1] == '--warmup-server':
        print(json.dumps(warmup_server()))
    elif len(sys.argv) == 2 and sys.argv[1] == '--health-server':
        print(json.dumps(server_health()))
    else:
        raise SystemExit('Usage: --worker REQUEST_JSON OUTPUT_MP4 | --server SOCKET | '
                         '--warmup-server | --health-server | --stop-server')
