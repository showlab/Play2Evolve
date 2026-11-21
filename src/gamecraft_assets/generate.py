"""Versioned, bounded H3 candidate generation. Reuses existing transport/image code."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .contract import (compile_generation_prompt, contract_instruction,
                       generation_contract, material_intent, reference_instruction)
from .plans import validate, write_json
from . import reference_image

JUDGE_SCHEMA = {'type': 'object', 'additionalProperties': False,
                'required': ['usable', 'identity', 'matte', 'failure'],
                'properties': {**{k: {'type': 'number'} for k in ['usable', 'identity', 'matte']},
                               'failure': {'type': 'string'}}}


@contextmanager
def _state_lock(root):
    with (Path(root)/'.generation-state.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def _append_history(root,rec):
    path=Path(root)/'asset_history.json'
    with _state_lock(root):
        rows=json.loads(path.read_text()) if path.exists() else []
        rows.append(rec);write_json(path,rows)


def _commit_manifest(root,asset,record):
    path=Path(root)/'asset_manifest.json'
    with _state_lock(root):
        manifest=json.loads(path.read_text())
        manifest['assets'][asset]=record
        write_json(path,manifest)


def acceptable(verdict, metrics, kind, thresholds=None):
    thresholds = thresholds or {}
    keys = ['usable'] if kind == 'background' else ['usable', 'identity', 'matte']
    if any(not isinstance(verdict.get(k), (int, float)) or not math.isfinite(verdict[k]) or
           not thresholds.get('min_' + k, .7) <= verdict[k] <= 1 for k in keys):
        return False
    return kind == 'background' or (metrics.get('coverage', 0) >= thresholds.get('min_coverage', .02) and metrics.get('in_frame', 0) >= thresholds.get('min_in_frame', .95)
        and (kind != 'animation' or metrics.get('bbox_fill', 1.0) < thresholds.get('max_bbox_fill', .85)))


def repair_viable(verdict, metrics, kind):
    """Gate only finite judgments and usable sprite geometry before game A/B."""
    return acceptable(verdict, metrics, kind, thresholds={
        'min_usable':0.0, 'min_identity':0.0, 'min_matte':0.0,
        'min_coverage':.005, 'min_in_frame':.75, 'max_bbox_fill':.95})


def _backend():
    # Only reuse transport, extraction and keying functions. Do NOT call its
    # game-specific generation, background judge, world optimizer or main().
    import gen_assets
    # A busy shared H3 server can leave a submitted job queued. Keep polling
    # that same job instead of treating its normal queue time as an asset error.
    gen_assets.H3_TIMEOUT = int(os.environ.get('GAMECRAFT_H3_JOB_TIMEOUT_SECONDS', '1200'))
    if gen_assets.H3_TIMEOUT < 1:
        raise ValueError('GAMECRAFT_H3_JOB_TIMEOUT_SECONDS must be positive')
    return gen_assets


def _h3_with_oom_wait(request):
    """Queue H3 calls and wait for transient GPU OOM without spending a new asset candidate."""
    max_wait = int(os.environ.get('GAMECRAFT_H3_OOM_WAIT_SECONDS', '600'))
    interval = int(os.environ.get('GAMECRAFT_H3_OOM_RETRY_SECONDS', '30'))
    if max_wait < 0 or interval < 1:
        raise ValueError('H3 OOM wait must be nonnegative and retry interval positive')
    # GameCraft processes on this host share one H3 service. Other clients may
    # use it too; their failed requests are handled by the OOM retry below.
    with open(Path(tempfile.gettempdir())/'gamecraft-h3-request.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deadline = time.monotonic() + max_wait
        while True:
            try:
                return request()
            except RuntimeError as exc:
                if 'out of memory' not in str(exc).lower():
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f'H3 remained out of memory after {max_wait}s of waiting') from exc
                pause = min(interval, remaining)
                print(f'H3 GPU out of memory; waiting {pause:.0f}s before retrying the same material request', flush=True)
                time.sleep(pause)


def _judge(g, asset, frames, ref=None):
    text = (f"Asset type: {asset['kind']}. Intended content: {asset['description']}. "
            f"Requirements: {asset['requirements']}. "
            'Score usable, identity and matte from 0 to 1. For a background, identity and matte are 1. '
            'For a static sprite without a reference, identity means matching its description. '
            'The supplied sprite samples have already been chroma-keyed and are intentionally composited on a '
            'neutral gray inspection background so transparency, holes and green spill are visible. Gray is not '
            'source content and must not be penalized. For sprites, matte means the visible subject has a clean, '
            'complete boundary without spill, opaque rectangles or missing body parts. '
            'Identify the most important fix in failure. Images are chronological samples; '
            'if a reference is appended it is labeled explicitly.')
    content = [{'type': 'input_text', 'text': text}]
    content += [g.llm.img(str(p)) for p in frames]
    if ref:
        content += [{'type': 'input_text', 'text': 'Identity reference:'}, g.llm.img(str(ref))]
    backend = os.environ.get('GAMECRAFT_JUDGE_BACKEND', 'local')
    model = os.environ.get('GAMECRAFT_JUDGE_MODEL', 'local')
    return g.llm.call(model, 'Judge a game asset against its stated purpose. Do not invent ground or character requirements.',
                      content, 'gamecraft_asset_judge', JUDGE_SCHEMA, backend=backend, max_tokens=800)



def _repair_prompt(g, asset, style, failures):
    """Translate diagnoses into positive generation instructions, not negative captions."""
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['prompt'],
              'properties': {'prompt': {'type': 'string'}}}
    backend = os.getenv('GAMECRAFT_ASSET_PROMPT_REPAIR_BACKEND', 'local')
    model = os.getenv('GAMECRAFT_ASSET_PROMPT_REPAIR_MODEL', 'local')
    if backend not in ('local', 'codex', 'api') or (backend != 'local' and model == 'local'):
        raise ValueError('Configure an explicit model for non-local asset prompt repair')
    result = g.llm.call(model,
        'Rewrite an H3 video generation prompt for a game asset. Preserve the subject identity and purpose. '
        'Use concrete positive visual instructions and fixed camera framing. Do not repeat descriptions of '
        'unwanted objects or turn a failure description into depicted content. Do not add output-format, '
        'chroma-key, transparency, margin, reference-frame or delivery instructions; the output contract '
        'adds those after this rewrite. For a skyline place distant buildings only along the bottom edge '
        'and broad open sky above. Output only the corrected generation prompt.',
        [{'type': 'input_text', 'text': json.dumps({'asset': asset, 'style': style, 'failures': failures[-3:]})}],
        'asset_prompt_repair', schema, backend=backend, max_tokens=900)
    return result['prompt']


def _h3_text(prompt, reference=False, loop=False, seconds=4):
    alignment = ('How the reference pictures align with the target video — Picture 1 aligns with 0.00 seconds' +
                 (f'; Picture 2 aligns with {seconds:.2f} seconds' if loop else '') + '.\n\n') if reference else ''
    return alignment + 'integrated_multimodal_description: [Shot 1] ' + prompt + '\n\noverall_soundscape: Quiet room tone.\n\nnon_diegetic_music: N/A'


def _reference_valid(g, path):
    if not Path(path).exists():
        return False
    import numpy as np
    from PIL import Image
    mask = g.key(np.asarray(Image.open(path).convert('RGB')))[..., 3] > 127
    if not mask.any() or not .02 < float(mask.mean()) < .7:
        return False
    ys, xs = np.where(mask)
    return float(mask.sum() / ((ys.max()-ys.min()+1)*(xs.max()-xs.min()+1))) < .85


def _existing_reference(root, plan, manifest, asset, dest):
    """Use a previously accepted material, never an arbitrary agent-supplied path."""
    from PIL import Image, ImageOps
    reference_id = asset['reference_asset_id']
    source_plan = next((item for item in plan['assets'] if item['id'] == reference_id), None)
    record = manifest['assets'].get(reference_id)
    if source_plan is None or record is None or not record.get('files'):
        raise ValueError(f'Reference material is not accepted: {reference_id}')
    frozen_self = (reference_id == asset['id'] and asset.get('incumbent_reference') is True)
    expected = hashlib.sha256(json.dumps(source_plan, sort_keys=True).encode()).hexdigest()
    if frozen_self:
        prefix = (Path('baseline_references') / reference_id).as_posix() + '/'
        if not all(name.startswith(prefix) for name in record['files']):
            raise ValueError(f'Incumbent reference is not a frozen baseline: {reference_id}')
    elif record.get('plan_sha256') != expected:
        raise ValueError(f'Reference material is stale: {reference_id}')
    source = (root / record['files'][0]).resolve()
    if root not in source.parents or not source.is_file():
        raise ValueError('Reference material path is missing or outside workspace')
    target = dest / 'reference.png'
    with Image.open(source) as image:
        rgba = image.convert('RGBA')
        # A subject can condition a complete scene. Match the H3 target aspect
        # without distorting its identity or cropping game-relevant pixels.
        scene = asset['kind'] == 'background'
        fill = (90, 90, 90, 255) if scene else (0, 255, 0, 255)
        background = Image.new('RGBA', rgba.size, fill)
        background.alpha_composite(rgba)
        output_size = (768, 432) if scene else (768, 768)
        ImageOps.pad(background.convert('RGB'), output_size,
                     color=fill[:3], centering=(.5, .5)).save(target)
    return target


def _process(g, video, asset, dest):
    import numpy as np
    from PIL import Image
    count = asset['frames']
    decoded = g.frames_at(str(video), count if count > 1 else 3)
    if count == 1:
        decoded = [decoded[1]]
    metrics = {'coverage': 1.0, 'in_frame': 1.0}
    if asset['kind'] != 'background':
        rgba = [g.key(f[..., :3]) for f in decoded]
        masks = [f[..., 3] > 127 for f in rgba]
        if any(not m.any() for m in masks):
            raise ValueError('Empty foreground after chroma key')
        metrics['coverage'] = float(min(m.mean() for m in masks))
        fills = []
        for mask in masks:
            ys, xs = np.where(mask)
            fills.append(float(mask.sum() / ((ys.max()-ys.min()+1)*(xs.max()-xs.min()+1))))
        metrics['bbox_fill'] = max(fills)
        metrics['in_frame'] = float(sum(not (m[:2].any() or m[-2:].any() or m[:, :2].any() or m[:, -2:].any()) for m in masks) / len(masks))
        # Shared crop across animation frames preserves relative positions and scale.
        union = np.logical_or.reduce(masks)
        ys, xs = np.where(union)
        pad = 8
        box = (max(0, int(xs.min())-pad), max(0, int(ys.min())-pad),
               min(union.shape[1], int(xs.max())+pad+1), min(union.shape[0], int(ys.max())+pad+1))
        images = [Image.fromarray(f).crop(box) for f in rgba]
    else:
        images = [Image.fromarray(f).convert('RGBA') for f in decoded]
    paths = []
    for i, image in enumerate(images):
        size = tuple(asset['size'])
        if asset['kind'] == 'background':
            canvas = image.resize(size, Image.Resampling.LANCZOS)
        else:
            image.thumbnail(size, Image.Resampling.LANCZOS)
            canvas = Image.new('RGBA', size)
            canvas.alpha_composite(image, ((size[0]-image.width)//2, (size[1]-image.height)//2))
        path = dest / f'frame{i:04d}.png'
        canvas.save(path)
        paths.append(path)
    # Judge on neutral gray to make transparency and residual green visible.
    samples = []
    for i in sorted({round(j*(len(paths)-1)/3) for j in range(4)}):
        image = Image.open(paths[i]).convert('RGBA')
        bg = Image.new('RGBA', image.size, (90, 90, 90, 255))
        bg.alpha_composite(image)
        path = dest / f'judge{i:04d}.png'
        bg.convert('RGB').save(path)
        samples.append(path)
    return paths, samples, metrics


def _process_still(source, asset, dest):
    """Apply the same game file contract and inspection view to a native still."""
    import numpy as np
    from PIL import Image, ImageOps

    with Image.open(source) as image:
        rgba = image.convert('RGBA')
    size = tuple(asset['size'])
    metrics = {'coverage': 1.0, 'in_frame': 1.0}
    if asset['kind'] == 'background':
        canvas = ImageOps.fit(rgba.convert('RGB'), size, method=Image.Resampling.LANCZOS).convert('RGBA')
    else:
        mask = np.asarray(rgba.getchannel('A')) > 127
        if not mask.any():
            raise ValueError('Empty foreground in generated still')
        metrics['coverage'] = float(mask.mean())
        metrics['in_frame'] = float(not (mask[:2].any() or mask[-2:].any() or
                                         mask[:, :2].any() or mask[:, -2:].any()))
        ys, xs = np.where(mask)
        metrics['bbox_fill'] = float(mask.sum() / ((ys.max()-ys.min()+1)*(xs.max()-xs.min()+1)))
        pad = 8
        cropped = rgba.crop((max(0, int(xs.min())-pad), max(0, int(ys.min())-pad),
                            min(rgba.width, int(xs.max())+pad+1),
                            min(rgba.height, int(ys.max())+pad+1)))
        cropped.thumbnail(size, Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', size)
        canvas.alpha_composite(cropped, ((size[0]-cropped.width)//2, (size[1]-cropped.height)//2))
    frame = dest / 'frame0000.png'
    canvas.save(frame)
    inspection = Image.new('RGBA', size, (90, 90, 90, 255))
    inspection.alpha_composite(canvas)
    sample = dest / 'judge0000.png'
    inspection.convert('RGB').save(sample)
    return [frame], [sample], metrics


def _still_prompt(prompt, contract):
    """Translate video-only green-screen delivery into a transparent image request."""
    old = 'Output requirements: ' + contract_instruction(contract)
    if old not in prompt:
        raise ValueError('Static material prompt is missing its output contract')
    clean = prompt.replace(old, '', 1)
    clauses = re.split(r'(?<=[.;])\s+', clean)
    clauses = [clause for clause in clauses if not (
        re.search(r'\b(?:green|chroma|#00ff00)\b', clause, re.IGNORECASE) and
        re.search(r'\b(?:background|screen|chroma|reserved)\b', clause, re.IGNORECASE))]
    return (' '.join(clauses).strip() + ' Output requirements: Render one isolated static foreground '
            'subject, fully visible with generous margins, on a transparent background for RGBA '
            'sprite extraction.')


def generate(workspace, only=None, rounds=2, candidates=2, *, validator=None, quality_gate=None, judge=True,
             video_skill=False, phase='initialization', feedback=None, auto_commit=True,
             extra_video_rules=(), propose_video_patch=True, context=None):
    root = Path(workspace).resolve()
    plan = json.loads((root / 'asset_plan.json').read_text())
    (validator or validate)(plan)
    gate = (quality_gate or acceptable) if judge else (lambda *_: True)
    if not judge:
        rounds, candidates = 1, 1
    if not 1 <= rounds <= 3 or not 1 <= candidates <= 4:
        raise ValueError('Bound budget: rounds 1–3, candidates 1–4')
    chosen = set(only or [a['id'] for a in plan['assets']])
    if chosen - {a['id'] for a in plan['assets']}:
        raise ValueError('Unknown asset id')
    # One lock per asset permits bounded cross-asset concurrency while still
    # preventing two rounds from regenerating the same asset simultaneously.
    with ExitStack() as locks:
        for asset_id in sorted(chosen):
            lock=locks.enter_context((root/f'.generation-{asset_id}.lock').open('a'))
            fcntl.flock(lock,fcntl.LOCK_EX)
        g = _backend()
        with _state_lock(root):
            manifest = json.loads((root / 'asset_manifest.json').read_text())
        history_path = root / 'asset_history.json'
        with _state_lock(root):
            history = json.loads(history_path.read_text()) if history_path.exists() else []
        for asset in plan['assets']:
            if asset['id'] not in chosen:
                continue
            asset_hash = hashlib.sha256(json.dumps(asset, sort_keys=True).encode()).hexdigest()
            accepted = manifest['assets'].get(asset['id'])
            if (accepted and accepted.get('plan_sha256') == asset_hash and
                    gate(accepted.get('verdict', {}), accepted.get('metrics', {}), asset['kind'])):
                print(f"{asset['id']}: already accepted; preserving version", flush=True)
                continue
            failures = [failure for r in history if r['asset'] == asset['id'] and not r.get('eligible')
                        if (failure := r.get('verdict', {}).get('failure', '').strip())]
            if feedback:
                failures.append(str(feedback))
            for round_index in range(rounds):
                style = asset.get('style', plan['style'])
                recipe = patch = None
                if video_skill:
                    from gamecraft_video_skill import resolve, propose_patch
                    # One patch/recipe per round: candidates differ only by seed.
                    if failures and propose_video_patch:
                        patch = propose_patch(g, asset, failures, workspace=root)
                    recipe = resolve(root, asset, style, phase=phase,
                                     extra_rules=[*extra_video_rules, *([patch] if patch else [])])
                batch = []
                contract = generation_contract(asset)
                scope, motion = material_intent(asset)
                seconds = float(asset.get('source_duration_seconds', 4))
                if seconds < 4 and os.environ.get('H3_BACKEND', 'local') != 'wan':
                    raise ValueError('Short source clips currently require H3_BACKEND=wan; local H3 needs 4 seconds')
                for _ in range(candidates):
                    # Versions are asset-local. Distinct assets may safely use
                    # the same number because their directories are separate.
                    version = max([0] + [int(p.name[1:]) for p in (root/'generated'/asset['id']).glob('v[0-9]*') if p.name[1:].isdigit()]) + 1
                    dest = root / 'generated' / asset['id'] / f'v{version:04d}'
                    dest.mkdir(parents=True, exist_ok=False)
                    note = '; '.join(x[:300] for x in failures[-2:] if x)
                    if video_skill:
                        prompt = recipe['prompt']
                    elif note and judge:
                        repaired = _repair_prompt(g, asset, style, failures)
                        prompt = compile_generation_prompt('', {**asset, 'prompt': repaired})
                    else:
                        prompt = compile_generation_prompt(style, asset)
                    still_image = (asset['kind'] in ('background', 'sprite') and
                                   asset['frames'] == 1 and reference_image.static_backend() == 'gpt_image')
                    if still_image and asset['kind'] == 'sprite':
                        prompt = _still_prompt(prompt, contract)
                    seed = 1000 + version
                    rec = {'asset': asset['id'], 'version': version, 'seed': seed, 'prompt': prompt,
                           'plan_sha256': asset_hash, 'accepted': False}
                    if context:
                        rec['context'] = dict(context)
                    if recipe:
                        rec.update(video_skill=True, recipe=recipe)
                        if patch:
                            rec['video_skill_patch'] = patch
                    write_json(dest / 'request.json', rec)
                    if recipe:
                        write_json(dest / 'recipe.json', recipe)
                    print(f"{asset['id']} v{version}: H3 generation", flush=True)
                    try:
                        video = dest / 'raw.mp4'
                        ref = None
                        if still_image:
                            config = reference_image.settings()
                            raw_image = dest / 'raw.png'
                            reference_image.generate_still(prompt, raw_image, kind=asset['kind'], config=config)
                            rec.update(image_backend='gpt_image', image_model=config['model'])
                            write_json(dest/'image_request.json', {
                                'prompt': prompt, 'kind': asset['kind'], 'model': config['model'],
                                'quality': config['quality'], 'base_url': config['base_url']})
                            paths, samples, metrics = _process_still(raw_image, asset, dest)
                        elif contract['method'] == 'fl2va':
                            if asset.get('reference_asset_id'):
                                ref = _existing_reference(root, plan, manifest, asset, dest)
                                rec['reference_asset_id'] = asset['reference_asset_id']
                            else:
                                ref = root / 'generated' / asset['id'] / 'reference.png'
                            # Reference cache is tied to the identity description, not corrective motion notes.
                                image_backend = reference_image.backend()
                                image_config = reference_image.settings(require_key=False) if image_backend == 'gpt_image' else None
                                ref_prompt = (asset['reference_prompt'].format(desc=asset['description']).strip()
                                              + ' Output requirements: ' + reference_instruction(
                                                  scope, transparent=image_backend == 'gpt_image'))
                                ref_meta = ref.with_suffix('.json')
                                expected_meta = {'prompt': ref_prompt, 'backend': image_backend}
                                if image_config:
                                    expected_meta.update(model=image_config['model'], quality=image_config['quality'],
                                                         base_url=image_config['base_url'])
                                cached_meta = json.loads(ref_meta.read_text()) if ref_meta.exists() else {}
                                if (not (_reference_valid(g, ref) if judge else ref.is_file()) or
                                        any(cached_meta.get(key) != value for key, value in expected_meta.items())):
                                    if image_backend == 'gpt_image':
                                        reference_image.generate(ref_prompt, ref, image_config)
                                    else:
                                        ref_video = dest / 'reference.mp4'
                                        reference_seconds = .2 if os.environ.get('H3_BACKEND', 'local') == 'wan' else 4
                                        _h3_with_oom_wait(lambda: g.h3_t2va(_h3_text(ref_prompt), seed, str(ref_video), aspect='1:1', seconds=reference_seconds))
                                        from PIL import Image
                                        Image.fromarray(g.frames_at(str(ref_video), 3)[1]).save(ref)
                                    write_json(ref_meta, {**expected_meta, 'seed': seed})
                                if judge and not _reference_valid(g, ref):
                                    raise ValueError('Reference is not an isolated sprite: opaque rectangular background or missing foreground')
                                if json.loads(ref_meta.read_text()).get('fit') != 'center-45pct':
                                    g.REF_FIT = (.5, .45)
                                    g.fit_ref(str(ref))
                                    meta = json.loads(ref_meta.read_text())
                                    meta['fit'] = 'center-45pct'
                                    write_json(ref_meta, meta)
                                archived_ref = dest / 'reference.png'
                                shutil.copyfile(ref, archived_ref)
                                ref = archived_ref
                                rec['reference_backend'] = image_backend
                                if image_config:
                                    rec['reference_model'] = image_config['model']
                            rec['reference'] = str(ref.relative_to(root))
                            write_json(dest / 'request.json', rec)
                            conditions = [{'type': 'image', 'uri': str(ref), 'role': 'keyframe', 'frame_index': 0}]
                            if motion == 'loop':
                                conditions += [{'type': 'image', 'uri': str(ref), 'role': 'keyframe', 'frame_index': -1}]
                            request = {'task': 'fl2va', 'prompt': _h3_text(prompt, reference=True, loop=motion == 'loop', seconds=seconds),
                                       'seed': seed, 'conditions': conditions, 'seconds': seconds,
                                       'target': {'short_edge': 768, 'aspect_ratio': contract['aspect_ratio'], 'duration_seconds': seconds}}
                            if recipe or 'generation_scope' in asset or 'source_duration_seconds' in asset:
                                write_json(dest/'h3_request.json', request)
                            _h3_with_oom_wait(lambda: g.h3_video(request, str(video)))
                        else:
                            if recipe or 'generation_scope' in asset or 'source_duration_seconds' in asset:
                                write_json(dest/'h3_request.json', {'task': 't2va', 'prompt': _h3_text(prompt),
                                           'seed': seed, 'conditions': [], 'seconds': seconds,
                                           'target': {'aspect_ratio': contract['aspect_ratio'], 'duration_seconds': seconds}})
                            if seconds == 4:
                                _h3_with_oom_wait(lambda: g.h3_t2va(_h3_text(prompt), seed, str(video), aspect=contract['aspect_ratio']))
                            else:
                                _h3_with_oom_wait(lambda: g.h3_t2va(_h3_text(prompt), seed, str(video), aspect=contract['aspect_ratio'], seconds=seconds))
                        if not still_image:
                            paths, samples, metrics = _process(g, video, asset, dest)
                        verdict = _judge(g, asset, samples, ref) if judge else {}
                        rec['quality_status'] = 'judged' if judge else 'not_evaluated'
                        rec.update(verdict=verdict, metrics=metrics, eligible=gate(verdict, metrics, asset['kind']),
                                   files=[str(p.relative_to(root)) for p in paths])
                        if rec['eligible']:
                            batch.append(rec)
                        else:
                            failures.append(verdict['failure'])
                    except Exception as e:
                        rec['error'] = f'{type(e).__name__}: {e}'
                    history.append(rec)
                    write_json(dest / 'result.json', rec)
                    _append_history(root,rec)
                if batch:
                    best = max(batch, key=lambda r: r['verdict'].get('usable', 0))
                    best['accepted'] = True
                    if video_skill and best.get('video_skill_patch') and auto_commit:
                        from gamecraft_video_skill import commit_patch
                        commit_patch(root, best['video_skill_patch'], evidence={'phase': phase, 'asset': asset['id'],
                                     'version': best['version'], 'verdict': best.get('verdict', {})})
                    record={**asset, 'version': best['version'], 'files': best['files'],
                            'plan_sha256': asset_hash, 'verdict': best['verdict'], 'metrics': best['metrics'],
                            'quality_status': best.get('quality_status', 'judged')}
                    manifest['assets'][asset['id']]=record
                    _commit_manifest(root,asset['id'],record)
                    write_json(root / 'generated' / asset['id'] / f"v{best['version']:04d}" / 'result.json', best)
                    print(f"{asset['id']}: accepted v{best['version']}", flush=True)
                    break
                print(f"{asset['id']}: no acceptable candidate in round {round_index+1}", flush=True)
        with _state_lock(root):
            manifest=json.loads((root/'asset_manifest.json').read_text())
        pending = sorted(a['id'] for a in plan['assets'] if a['id'] in chosen and
                         (a['id'] not in manifest['assets'] or manifest['assets'][a['id']].get('plan_sha256') !=
                          hashlib.sha256(json.dumps(a, sort_keys=True).encode()).hexdigest() or
                          not gate(manifest['assets'][a['id']].get('verdict', {}),
                                         manifest['assets'][a['id']].get('metrics', {}), a['kind'])))
        with _state_lock(root):
            write_json(root/'generation_status.json',{'pending':pending,'accepted':sorted(manifest['assets'])})
        return pending
