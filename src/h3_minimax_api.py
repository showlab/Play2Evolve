"""MiniMax Open Platform adapter for the project's SGLang-style H3 requests.

Select explicitly with H3_BACKEND=minimax. The API key is read at request time
from MINIMAX_API_KEY and is never written to generation records.
"""

import base64
import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_BASE = 'https://api.minimax.io'


def _json(method, path, key, body=None, timeout=60):
    base = os.environ.get('MINIMAX_API_BASE', API_BASE).rstrip('/')
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Authorization': f'Bearer {key}',
                                              'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors='replace')[:1000]
        raise RuntimeError(f'MiniMax H3 {method} {path} failed (HTTP {exc.code}): {detail}') from exc


def _image_url(uri):
    if uri.startswith(('https://', 'http://', 'data:image/')):
        return uri
    path = Path(uri).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f'H3 keyframe image not found: {path}')
    mime = mimetypes.guess_type(path.name)[0]
    if mime not in ('image/png', 'image/jpeg', 'image/webp'):
        raise ValueError(f'Unsupported MiniMax H3 keyframe format: {path.suffix}')
    if path.stat().st_size > 30 * 1024 * 1024:
        raise ValueError(f'MiniMax H3 keyframe exceeds 30 MB: {path}')
    return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')


def api_request(body):
    """Map t2va/fl2va and first/last keyframes to MiniMax's V2 schema."""
    task = body.get('task')
    if task not in ('t2va', 'fl2va'):
        raise ValueError(f'MiniMax H3 backend does not support task {task!r}')
    prompt = body.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('MiniMax H3 requires a nonempty prompt')
    conditions = body.get('conditions') or []
    if task == 't2va' and conditions:
        raise ValueError('t2va cannot contain keyframe images')
    if task == 'fl2va' and not conditions:
        raise ValueError('fl2va requires at least one keyframe image')
    content = [{'type': 'text', 'text': prompt}]
    seen = set()
    for condition in conditions:
        if condition.get('type') != 'image' or condition.get('role') != 'keyframe':
            raise ValueError('MiniMax H3 backend supports only image keyframes')
        frame_index = condition.get('frame_index')
        role = {0: 'first_frame', -1: 'last_frame'}.get(frame_index)
        if role is None or role in seen:
            raise ValueError('MiniMax H3 supports one first and one last frame')
        seen.add(role)
        content.append({'type': 'image_url', 'image_url': {'url': _image_url(condition['uri'])},
                        'role': role})
    target = body.get('target') or {}
    duration = body.get('seconds', target.get('duration_seconds', 4))
    if type(duration) is not int or not 4 <= duration <= 15:
        raise ValueError('MiniMax-H3 duration must be an integer from 4 to 15 seconds')
    short_edge = target.get('short_edge', 768)
    if short_edge != 768:
        raise ValueError('MiniMax H3 adapter currently supports only 768P output')
    ratio = target.get('aspect_ratio', '16:9')
    if task == 't2va' and ratio not in ('21:9', '16:9', '4:3', '1:1', '3:4', '9:16'):
        raise ValueError(f'Unsupported MiniMax H3 text-to-video aspect ratio: {ratio}')
    # The hosted API derives I2V's ratio from its keyframe image.
    return {'model': 'MiniMax-H3', 'content': content, 'resolution': '768P',
            'duration': duration, 'ratio': ratio if task == 't2va' else 'adaptive'}


def generate(body, out_mp4, timeout=600):
    key = os.environ.get('MINIMAX_API_KEY')
    if not key:
        raise RuntimeError('H3_BACKEND=minimax requires MINIMAX_API_KEY on the machine running asset generation')
    payload = api_request(body)
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    if len(encoded) > 64 * 1024 * 1024:
        raise ValueError('MiniMax H3 request exceeds the 64 MB API body limit')
    request_sha256 = hashlib.sha256(encoded).hexdigest()
    output = Path(out_mp4)
    task_file = output.with_suffix('.minimax_task.json')
    if task_file.is_file():
        saved = json.loads(task_file.read_text())
        if saved.get('request_sha256') != request_sha256:
            raise RuntimeError(f'Existing MiniMax task has a different request: {task_file}')
        task_id = saved['task_id']
    else:
        result = _json('POST', '/v2/video_generation', key, payload, timeout=120)
        task_id = result.get('task_id')
        if not task_id:
            raise RuntimeError(f'MiniMax H3 did not return a task_id: {str(result)[:500]}')
        output.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(json.dumps({'task_id': task_id, 'request_sha256': request_sha256}))
    deadline = time.monotonic() + timeout
    while True:
        task = _json('GET', '/v2/query/video_generation/' + urllib.parse.quote(str(task_id), safe=''), key)
        task = task.get('task', {})
        status = task.get('status')
        if status == 'succeeded':
            url = (task.get('content') or {}).get('url')
            if not url or not url.startswith('https://'):
                raise RuntimeError(f'MiniMax H3 task {task_id} returned no HTTPS video URL')
            try:
                with urllib.request.urlopen(url, timeout=600) as response, output.open('wb') as dest:
                    while chunk := response.read(1024 * 1024):
                        dest.write(chunk)
            except Exception:
                output.unlink(missing_ok=True)
                raise
            return
        if status in ('failed', 'cancelled', 'canceled'):
            raise RuntimeError(f'MiniMax H3 task {task_id} {status}: {str(task)[:1000]}')
        if status not in ('queued', 'running', 'preparing', 'processing'):
            raise RuntimeError(f'MiniMax H3 task {task_id} returned unknown status {status!r}')
        if time.monotonic() >= deadline:
            raise TimeoutError(f'MiniMax H3 task {task_id} still {status} after {timeout}s; resume from {task_file}')
        time.sleep(min(5, max(0, deadline - time.monotonic())))
