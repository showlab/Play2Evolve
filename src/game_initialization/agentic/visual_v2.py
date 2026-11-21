"""Independent, image-grounded basic visual integration acceptance."""
import json
import subprocess
from pathlib import Path
from PIL import Image
from game_initialization.batch.__main__ import codex, inside, read, write, sha, command
from game_initialization.package import inventory
from .requirements import object_schema, STRING, STRINGS

SCHEMA = object_schema({
    'passed': {'type': 'boolean'},
    'observations': {'type': 'array', 'items': object_schema({
        'evidence': STRING, 'observation': STRING})},
    'issues': {'type': 'array', 'items': object_schema({
        'id': STRING, 'blocking': {'type': 'boolean'},
        'category': STRING, 'problem': STRING,
        'evidence_refs': STRINGS, 'locations': STRINGS,
        'repair_side': {'type': 'string', 'enum': ['asset', 'code', 'both']},
        'asset_ids': STRINGS, 'suggested_repair': STRING})},
    'limitations': STRINGS})

PROMPT = '''You are an independent basic visual integration judge for game initialization.
Read instruction.md and the evidence manifest below. ACTUALLY inspect every attached
contact sheet (four chronological frames each), not just author claims or counters.
Read corresponding scenario decisions/result.json and game code to check ambiguous
interaction timing. You may open additional existing frames with image tools.
Do not edit files or run training. Never invent unseen events or unsupported defects.
Judge basic playability and scene-role correctness, without beauty scores:
- same character identity/clothes/scale across actions; intended actor count;
- isolated actors/props, no baked scene rectangles, residual green screen on subjects,
  duplicate people/body fragments, severe cropping/occlusion or invisible essential UI;
- actions belong to their state, correct orientation/placement and action semantics;
- visible contact/reach agrees with damage; hit/block/prop response agrees with input
  and state, e.g. an interactive struck object should visibly respond as appropriate;
- essential state changes have visible feedback and can be understood by a player.
A basic error blocks publication. Minor style, smoothness, subtle anatomy, polish,
balance and player strategy are deferred to RSI. Absence of sufficient evidence for
an essential interaction must be a blocking coverage issue, not assumed success.
For each issue put one or more exact manifest paths in evidence_refs (paths only;
no prose), and put timestamps/tile positions in locations. Give a concrete
observation and repair_side (asset/code/both); asset_ids must name existing plan IDs.
Recommend targeted H3 regeneration for incorrect source content and code changes for
placement, input/animation/damage synchronization or prop reactions. Preserve originals.
Give an observation for EVERY attached sheet. passed=true only without blocking issues.
This is a sampled-frame visual check, not proof of all frames or official evaluation.
Evidence manifest:\n'''


def collect_evidence(root, runtime, stage):
    package = inside(root, runtime['package'])
    safe_stage = ''.join(c if c.isalnum() or c in '-_' else '_' for c in stage)
    if not safe_stage:
        raise ValueError('Visual review stage must have a path-safe identity')
    out = package.parent/f'visual-evidence-{safe_stage}'
    out.mkdir(exist_ok=False)
    rows = []
    for video in sorted((package/'checks').glob('*/frame-replay/replay.mp4')):
        duration = float(subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(video)], text=True))
        if duration <= 0:
            raise ValueError('Empty replay video')
        times = [duration*f for f in (.05, .30, .60, .90)]
        frames = []
        for i, t in enumerate(times):
            frame = out/f'{video.parents[1].name}-{i}.png'
            command(['ffmpeg', '-y', '-ss', str(t), '-i', str(video),
                     '-frames:v', '1', str(frame)], out/f'{frame.stem}.log', timeout=60)
            with Image.open(frame) as im:
                frames.append(im.convert('RGB').resize((640, 360)))
        sheet = Image.new('RGB', (1280, 720))
        for i, im in enumerate(frames):
            sheet.paste(im, ((i % 2)*640, (i // 2)*360))
        path = out/f'{video.parents[1].name}-sheet.png'
        sheet.save(path)
        result = video.parents[1]/'result.json'
        rows.append({'image': str(path.relative_to(root)), 'video': str(video.relative_to(root)),
                     'result': str(result.relative_to(root)), 'times_seconds': times,
                     'tile_order': 'top-left, top-right, bottom-left, bottom-right'})
    if not rows:
        raise ValueError('No replay evidence for visual review')
    write(out/'manifest.json', rows)
    return rows


def validate_review(root, report, evidence):
    expected = {r['image'] for r in evidence}
    observed = [r['evidence'] for r in report['observations']]
    if set(observed) != expected or len(observed) != len(expected):
        raise ValueError('Visual review did not inspect every replay sheet')
    if any(not r['observation'].strip() for r in report['observations']):
        raise ValueError('Empty visual observation')
    allowed = expected | {r['video'] for r in evidence} | {r['result'] for r in evidence}
    ids = {a['id'] for a in read(root/'generation/asset_plan.json')['assets']}
    for issue in report['issues']:
        if (not issue['problem'].strip() or not issue['suggested_repair'].strip()
                or not issue['evidence_refs'] or not set(issue['evidence_refs']).issubset(allowed)
                or not set(issue['asset_ids']).issubset(ids)):
            raise ValueError('Visual issue must cite current evidence and known assets')
    if report['passed'] is not True or any(r['blocking'] for r in report['issues']):
        raise ValueError('Basic visual integration rejected: '+json.dumps(report['issues'], ensure_ascii=False))


def review_visual(root, cfg, stage, runtime):
    before = inventory(root/'draft')
    package = inside(root, runtime['package'])
    package_before = inventory(package)
    evidence = collect_evidence(root, runtime, stage)
    image_hashes = {r['image']: sha(inside(root, r['image'])) for r in evidence}
    report = codex(root, cfg, stage, PROMPT+json.dumps(evidence), schema=SCHEMA,
                   images=[inside(root, r['image']) for r in evidence])
    if (before != inventory(root/'draft') or package_before != inventory(package)
            or image_hashes != {r['image']: sha(inside(root, r['image'])) for r in evidence}):
        raise ValueError('Read-only visual reviewer changed source or evidence')
    write(root/'latest_visual_review.json', {'review': report, 'evidence': evidence,
          'images_sha256': image_hashes, 'draft_sha256': before,
          'package_sha256': package_before, 'runtime_sha256': sha(root/'latest_check.json')})
    # The visual reviewer itself inspected the current frames, so use its
    # observations as the canonical inspection record instead of requiring a
    # second, separately authored file from the builder.
    write(root/'inspection.json', {
        'screens': [{'name': Path(row['image']).stem, 'image': row['image'],
                     'observation': next((x['observation'] for x in report['observations']
                                          if x['evidence'] == row['image']), '')}
                    for row in evidence],
        'videos': [{'path': row['video'], 'observation': next(
                       (x['observation'] for x in report['observations']
                        if x['evidence'] == row['image']), '')}
                   for row in evidence]})
    validate_review(root, report, evidence)


def require_current_visual_review(root, runtime):
    if not (root/'latest_visual_review.json').is_file():
        raise ValueError('Independent basic visual review is required before publication')
    saved = read(root/'latest_visual_review.json')
    if (saved['draft_sha256'] != inventory(root/'draft')
            or saved['package_sha256'] != inventory(inside(root, runtime['package']))
            or saved['runtime_sha256'] != sha(root/'latest_check.json')
            or any(sha(inside(root, k)) != v for k, v in saved['images_sha256'].items())):
        raise ValueError('No current visual review')
    validate_review(root, saved['review'], saved['evidence'])
