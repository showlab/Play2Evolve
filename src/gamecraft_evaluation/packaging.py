"""Freeze an initial game and package predeclared demos without score selection."""
import hashlib
import json
import shutil
from pathlib import Path

from gamecraft_behavior.core import digest, write_json
from gamecraft_composition.core import snapshot

TASK = 'platformer-dread-wings'


def read_json(path):
    return json.loads(Path(path).read_text())


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def content_digest(project):
    # Godot imports may create metadata; game code, resources and traces stay fixed.
    return {k: v for k, v in digest(project).items()
            if Path(k).suffix not in {'.uid', '.import'}}


def check_frozen(run):
    run = Path(run)
    config = read_json(run / 'run.json')
    if content_digest(run / 'initial-project') != config['initial_digest']:
        raise ValueError('Frozen initial project changed; create a new evaluation run')
    return config


def check_implementation(config):
    root = Path(__file__).resolve().parents[1]
    changed = [name for name, expected in config['implementation_sha256'].items()
               if not (root / name).is_file() or file_hash(root / name) != expected]
    if changed:
        raise ValueError(f'Code changed after protocol freeze; prepare a new run: {changed}')


def validate_trace(path):
    data = read_json(path)
    duration = data.get('duration_frames')
    if type(duration) is not int or not 1 <= duration <= 600:
        raise ValueError(f'Invalid demo duration: {path}')
    events = data.get('events')
    if not isinstance(events, list):
        raise ValueError(f'Demo needs an events list: {path}')
    last = -1
    for event in events:
        frame = event.get('frame')
        if type(frame) is not int or not last <= frame < duration:
            raise ValueError(f'Invalid demo event frame: {path}')
        if event.get('type') not in {'key_press', 'key_down', 'key_up', 'mouse_click',
                                      'mouse_down', 'mouse_up', 'mouse_move', 'wait'}:
            raise ValueError(f'Unsupported demo event: {path}')
        last = frame
    return data


def prepare(project, output, *, seeds, step_frames, max_decisions, backend, model, player_label=None):
    project, output = Path(project).resolve(), Path(output).resolve()
    if project == output or project in output.parents or output in project.parents:
        raise ValueError('Output must be separate from the source project')
    if not (project / 'project.godot').is_file():
        raise ValueError('Expected an existing Godot project')
    if read_json(project / 'game_plan.json').get('task_id') != TASK:
        raise ValueError('Only the Dread Wings template is currently supported')
    if not 1 <= len(seeds) <= 9 or len(set(seeds)) != len(seeds) or any(s < 0 for s in seeds):
        raise ValueError('Provide 1–9 distinct nonnegative seeds (plus one title demo)')
    if not 1 <= step_frames <= 30 or max_decisions < 1 or step_frames * max_decisions > 600:
        raise ValueError('Player budget must fit within 600 trace frames')
    if backend != 'local' and model == 'local':
        raise ValueError('Choose an explicit player model for api/codex')
    title = project / 'demo_outputs/01_title_flow.json'
    if validate_trace(title).get('scenario') not in (None, 'title_flow'):
        raise ValueError('Expected the original title-flow demo')
    if 'BehaviorBridge=' in (project / 'project.godot').read_text() or (project / 'behavior_bridge.gd').exists():
        raise ValueError('Use the clean game, not a behavior runtime-project')
    original = digest(project)
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / 'initial-project'
    snapshot(project, frozen)
    # Existing helper changes scenario dispatch in the private copy only.
    from gamecraft_training.seeds import install
    install(frozen)
    if digest(project) != original:
        raise ValueError('Source changed while freezing; do not use this snapshot')
    code = Path(__file__).resolve().parents[1]
    implementation = {}
    for package in ('gamecraft_evaluation', 'gamecraft_behavior', 'gamecraft_composition'):
        for path in sorted((code / package).rglob('*')):
            if path.is_file() and path.suffix in {'.py', '.gd'}:
                implementation[str(path.relative_to(code))] = file_hash(path)
    for rel in ('llm.py', 'gamecraft_training/seeds.py', 'gamecraft_training/replay.py'):
        implementation[rel] = file_hash(code / rel)
    write_json(output / 'run.json', {
        'protocol': 'frozen-initial-assets-zero-optimization-v1', 'task': TASK,
        'source_project': str(project), 'source_digest': original,
        'initial_digest': content_digest(frozen), 'implementation_sha256': implementation,
        'skill': '', 'optimization_rounds': 0, 'player_backend': backend, 'player_model': model,
        'player_checkpoint_label': player_label or 'unspecified; model alias alone does not identify weights',
        'seeds': seeds, 'step_frames': step_frames, 'max_decisions': max_decisions,
        'demo_policy': 'original title trace plus one empty-skill episode per declared seed; no ranking',
        'scope': 'one authored game; existing assets may already have been refined; not one-shot generation',
        'scenario_adaptation': 'gamecraft_training.seeds.install on private copy',
    })
    (output / 'initial_skill.txt').write_text('')
    return output


def package_demos(run, results):
    run = Path(run).resolve()
    config = check_frozen(run)
    frozen = run / 'initial-project'
    if len(results) != len(config['seeds']):
        raise ValueError('Every predeclared seed must have a rollout; no dropping failed episodes')
    entries = [('00_title_flow.json', frozen / 'demo_outputs/01_title_flow.json')]
    for index, (seed, result) in enumerate(zip(config['seeds'], results), 1):
        if not result.get('replay_verified') or result['seed'] != seed:
            raise ValueError('Cannot package an unverified or mismatched rollout')
        if result['project_sha256'] != digest(frozen) or result['decision_frames'] != config['step_frames']:
            raise ValueError('Rollout does not belong to this frozen project and decision interval')
        trace_path = run / 'rollouts' / f'seed-{seed}' / 'trace.json'
        if Path(result['trace']).resolve() != trace_path:
            raise ValueError('Unexpected rollout trace location')
        trace_data = validate_trace(trace_path)
        if trace_data.get('scenario') != f'ready_seed_{seed}':
            raise ValueError('Trace scenario does not match its declared seed')
        if trace_data['duration_frames'] > config['step_frames'] * config['max_decisions']:
            raise ValueError('Trace exceeds the declared decision budget')
        entries.append((f'{index:02d}_seed_{seed}.json', trace_path))
    for _, source in entries:
        validate_trace(source)
    # Build a clean submission from the frozen game, never the instrumented copy.
    submission = run / 'submission'
    shutil.copytree(frozen, submission,
                    ignore=shutil.ignore_patterns('.git', '.godot', '__pycache__', 'demo_outputs'))
    demos = submission / 'demo_outputs'
    demos.mkdir()
    for name, source in entries:
        shutil.copyfile(source, demos / name)
    write_json(run / 'submission.json', {
        'project': str(submission), 'digest': content_digest(submission),
        'demos': [{'name': name, 'source': str(source), 'sha256': file_hash(source)}
                  for name, source in entries],
    })
    return submission
