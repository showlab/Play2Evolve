from __future__ import annotations
import hashlib
import json
import math
import re
import shutil
from pathlib import Path

CASE = Path(__file__).parent / 'cases' / 'dread_wings'


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    tmp.replace(path)


def validate(plan):
    if plan.get('schema_version') != 1 or plan.get('task_id') != 'platformer-dread-wings':
        raise ValueError('This first adapter supports only platformer-dread-wings v1')
    validate_generic(plan, strict=False)
    seen = {a['id'] for a in plan['assets']}
    if seen != {'bird', 'pipe', 'skyline'}:
        raise ValueError('Dread Wings requires bird, pipe and skyline')


def initialize(instruction, output):
    instruction, output = Path(instruction), Path(output)
    raw = instruction.read_bytes()
    if 'Dread Wings' not in raw.decode() or instruction.name != 'instruction.md':
        raise ValueError('Expected the Dread Wings public instruction.md')
    plan = json.loads((CASE / 'asset_plan.json').read_text())
    validate(plan)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(instruction, output / 'instruction.md')
    for name in ['game_plan.json', 'asset_plan.json']:
        shutil.copyfile(CASE / name, output / name)
    write_json(output / 'provenance.json', {'instruction_sha256': hashlib.sha256(raw).hexdigest(),
               'source': str(instruction.resolve()), 'planner': 'case-specific authored design v1',
               'official_tests_read': False})
    write_json(output / 'asset_manifest.json', {'schema_version': 1, 'task_id': plan['task_id'], 'assets': {}})
    return output


def revise(workspace, asset_id, feedback):
    """Explicit rollout-driven material revision; previously accepted files survive."""
    import fcntl
    root = Path(workspace).resolve()
    feedback = str(feedback).strip()
    if not feedback or len(feedback) > 2000:
        raise ValueError('Feedback must contain 1–2000 characters')
    with (root / '.generation.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = json.loads((root / 'asset_plan.json').read_text())
        validate(plan)
        asset = next((a for a in plan['assets'] if a['id'] == asset_id), None)
        if asset is None:
            raise ValueError('Unknown asset id')
        history_path = root / 'plan_revisions.json'
        history = json.loads(history_path.read_text()) if history_path.exists() else []
        history.append({'asset': asset_id, 'old_prompt': asset['prompt'], 'feedback': feedback})
        asset['prompt'] += ' Correction requested after evaluation: ' + feedback
        write_json(history_path, history)
        write_json(root / 'asset_plan.json', plan)


def validate_generic(plan, strict=True):
    """Task-neutral schema used only by initialized game packages."""
    from .contract import material_intent
    if plan.get("schema_version") != 1 or not plan.get("task_id"):
        raise ValueError("Expected schema_version=1 and task_id")
    seen = set()
    for a in plan['assets']:
        if not re.fullmatch(r'[a-z][a-z0-9_]*', a['id']) or a['id'] in seen:
            raise ValueError('Asset ids must be unique path-safe identifiers')
        seen.add(a['id'])
        if a['kind'] not in {'animation', 'sprite', 'background'}:
            raise ValueError('Unknown asset kind')
        scope, motion = material_intent(a)
        if (a['kind'] == 'background') != (scope == 'scene'):
            raise ValueError('Scene scope requires background kind; foreground scope requires '
                             f"sprite or animation: asset {a['id']!r} has kind={a['kind']!r}, "
                             f'scope={scope!r}')
        if a['kind'] == 'sprite' and motion != 'static':
            raise ValueError('Sprite motion must be static')
        if a['kind'] == 'animation' and motion == 'static':
            raise ValueError('Animation motion must be loop or event')
        if scope == 'coupled_action' and a['kind'] != 'animation':
            raise ValueError('Coupled action requires an animation')
        if 'motion' in a and a['kind'] == 'animation' and a.get('loop') is not None:
            if (motion == 'loop') != a['loop']:
                raise ValueError('Animation motion and loop disagree')
        if 'generation_scope' in a:
            if not isinstance(a.get('scope_reason'), str) or not a['scope_reason'].strip():
                raise ValueError('Explicit generation scope needs a reason')
            if not isinstance(a.get('game_controlled'), list) or any(not isinstance(x, str) or not x.strip() for x in a['game_controlled']):
                raise ValueError('Explicit generation scope needs a game_controlled list')
        if scope == 'coupled_action' and (not isinstance(a.get('includes'), list) or
                len(a['includes']) < 2 or any(not isinstance(x, str) or not x.strip() for x in a['includes'])):
            raise ValueError('Coupled action must name at least two included subjects or props')
        reference_id=a.get('reference_asset_id')
        if 'reference_asset_id' in a:
            if motion == 'static':
                raise ValueError(f"Static asset {a['id']!r} cannot use reference_asset_id")
            if not isinstance(reference_id, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', reference_id):
                raise ValueError(f"Asset {a['id']!r} has an invalid reference_asset_id")
        if a.get('incumbent_reference') is True:
            if reference_id != a['id'] or motion == 'static':
                raise ValueError('Incumbent reference requires a moving asset referencing its own ID')
        elif reference_id == a['id']:
            raise ValueError('Self reference requires a frozen incumbent')
        if len(a['size']) != 2 or any(type(n) is not int or not 16 <= n <= 2048 for n in a['size']):
            raise ValueError('Invalid output size')
        if not 1 <= a['frames'] <= 60 or not 1 <= a['fps'] <= 60:
            raise ValueError('Invalid animation length/rate')
        if (motion == 'static') != (a['frames'] == 1):
            raise ValueError('Static material needs one frame; moving material needs multiple frames')
        if 'source_duration_seconds' in a:
            duration = a['source_duration_seconds']
            if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
                    not math.isfinite(duration) or not .2 <= duration <= 4):
                raise ValueError('source_duration_seconds must be between 0.2 and 4 seconds')
        if not a.get('prompt') or not a.get('requirements'):
            raise ValueError('Every asset needs prompt and task-specific judge requirements')
    if not seen:
        raise ValueError('Asset plan must not be empty')
    if not strict:
        return
    for a in plan['assets']:
        if not a.get('description'):
            raise ValueError('Assets need a description')
        if a['kind'] == 'animation' and (not (a.get('reference_prompt') or a.get('reference_asset_id')) or type(a.get('loop')) is not bool):
            raise ValueError('Animations need an identity reference and loop')
