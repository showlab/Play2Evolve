"""Optional still-image source for Wan first-frame conditioning."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path


def backend() -> str:
    """Keep the original video-reference path for H3 and explicit opt-outs."""
    selected = os.getenv('GAMECRAFT_REFERENCE_BACKEND')
    if selected is None:
        selected = ('gpt_image' if os.getenv('H3_BACKEND') == 'wan' and
                    os.getenv('WAN_MODEL_VARIANT') == 'i2v_a14b_distill4' else 'video')
    if selected not in ('video', 'gpt_image'):
        raise ValueError('GAMECRAFT_REFERENCE_BACKEND must be video or gpt_image')
    return selected


def static_backend() -> str:
    """Choose a still-image source for static materials; retain video elsewhere."""
    selected = os.getenv('GAMECRAFT_STATIC_IMAGE_BACKEND')
    if selected is None:
        selected = ('gpt_image' if os.getenv('H3_BACKEND') == 'wan' and
                    os.getenv('WAN_MODEL_VARIANT') == 'i2v_a14b_distill4' else 'video')
    if selected not in ('video', 'gpt_image'):
        raise ValueError('GAMECRAFT_STATIC_IMAGE_BACKEND must be video or gpt_image')
    return selected


def settings(*, require_key: bool = True) -> dict[str, str | None]:
    key = os.getenv('GAMECRAFT_IMAGE_API_KEY')
    key_path = os.getenv('GAMECRAFT_IMAGE_API_KEY_FILE')
    key_file = Path(key_path).expanduser() if key_path else None
    if not key and key_file is not None and key_file.is_file():
        key = key_file.read_text().strip()
    if require_key and not key:
        raise RuntimeError('Set GAMECRAFT_IMAGE_API_KEY or GAMECRAFT_IMAGE_API_KEY_FILE '
                           'to generate Wan reference images, '
                           'or set GAMECRAFT_REFERENCE_BACKEND=video to use the previous path')
    base_url = os.getenv('GAMECRAFT_IMAGE_API_BASE_URL', '').rstrip('/')
    if not base_url.startswith('https://'):
        raise ValueError('Set GAMECRAFT_IMAGE_API_BASE_URL to an explicit HTTPS endpoint')
    model = os.getenv('GAMECRAFT_IMAGE_MODEL', 'gpt-image-2.5-flare')
    quality = os.getenv('GAMECRAFT_IMAGE_QUALITY', 'medium')
    if quality not in ('low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('GAMECRAFT_IMAGE_QUALITY must be low, medium, high, xhigh, or max')
    return {'key': key, 'base_url': base_url, 'model': model, 'quality': quality}


def generate(prompt: str, path: str | Path, config: dict[str, str | None] | None = None) -> None:
    """Generate an isolated subject, then flatten its alpha to the game's green matte."""
    from openai import OpenAI
    from PIL import Image

    config = config or settings()
    if not config.get('key'):
        raise RuntimeError('Configure the image gateway key to generate a new reference image')
    client = OpenAI(api_key=config['key'], base_url=config['base_url'])
    response = client.images.generate(model=config['model'], prompt=prompt, size='1024x1024',
                                      quality=config['quality'], background='transparent', n=1)
    if not response.data or not response.data[0].b64_json:
        raise RuntimeError('Image API returned no image bytes')
    raw = base64.b64decode(response.data[0].b64_json, validate=True)
    with Image.open(io.BytesIO(raw)) as source:
        source.load()
        rgba = source.convert('RGBA')
    if rgba.getextrema()[3][0] == 255:
        raise ValueError('Image API did not produce a transparent foreground reference')
    matte = Image.new('RGBA', rgba.size, (0, 255, 0, 255))
    matte.alpha_composite(rgba)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.tmp')
    try:
        matte.convert('RGB').save(temporary, format='PNG')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def generate_still(prompt: str, path: str | Path, *, kind: str,
                   config: dict[str, str | None] | None = None) -> None:
    """Save the image model's native alpha/opaque output for a static game asset."""
    if kind not in ('background', 'sprite'):
        raise ValueError('Image generation supports only static backgrounds and sprites')
    from openai import OpenAI
    from PIL import Image

    config = config or settings()
    if not config.get('key'):
        raise RuntimeError('Configure the image gateway key to generate a static asset')
    response = OpenAI(api_key=config['key'], base_url=config['base_url']).images.generate(
        model=config['model'], prompt=prompt,
        size='1536x1024' if kind == 'background' else '1024x1024',
        quality=config['quality'],
        background='opaque' if kind == 'background' else 'transparent', n=1)
    if not response.data or not response.data[0].b64_json:
        raise RuntimeError('Image API returned no image bytes')
    raw = base64.b64decode(response.data[0].b64_json, validate=True)
    with Image.open(io.BytesIO(raw)) as source:
        source.load()
        rgba = source.convert('RGBA')
    if kind == 'sprite' and rgba.getchannel('A').getextrema()[0] == 255:
        raise ValueError('Image API did not produce a transparent sprite')
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.tmp')
    try:
        rgba.save(temporary, format='PNG')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
