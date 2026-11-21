from __future__ import annotations
import hashlib
import json
import shutil
from pathlib import Path


def write_json(path, value):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')
    tmp.replace(p)


def digest(project):
    return {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in Path(project).rglob('*') if p.is_file() and
            not any(x in {'.godot', '.git', '__pycache__'} for x in p.relative_to(project).parts)}


def prepare(project, target, adapter=None):
    project, target = Path(project).resolve(), Path(target).resolve()
    if project == target or project in target.parents:
        raise ValueError('Runtime copy must be outside source project')
    if any(p.is_symlink() for p in project.rglob('*')):
        raise ValueError('Project symlinks are not supported')
    from .adapters import resolve
    adapter = resolve(project, adapter)
    config_text = (project / 'project.godot').read_text()
    if 'BehaviorBridge=' in config_text:
        raise ValueError('Input project already contains the development bridge')
    if 'common/physics_ticks_per_second=' in config_text:
        import re
        setting = re.search(r'common/physics_ticks_per_second=([0-9]+)', config_text)
        if setting is None or int(setting.group(1)) != 60:
            raise ValueError('This adapter requires 60 Hz game physics')
    shutil.copytree(project, target, ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
    shutil.copyfile(Path(__file__).with_name('bridge.gd'), target / 'behavior_bridge.gd')
    shutil.copyfile(adapter.bridge_script, target / 'behavior_game_adapter.gd')
    config = target / 'project.godot'
    text = config.read_text()
    entry = 'BehaviorBridge="*res://behavior_bridge.gd"\n'
    if '[autoload]' in text:
        text = text.replace('[autoload]', '[autoload]\n' + entry, 1)
    else:
        text += '\n[autoload]\n' + entry
    config.write_text(text)
    return target


def action(value, adapter=None):
    from .adapters import resolve
    return resolve(adapter=adapter).validate_action(value)


class HeldKeyState:
    """Turn held-key requests into transitions for one playtest episode."""

    def __init__(self):
        self.held_keys = set()

    def dispatch(self, events):
        emitted, issues = [], []
        for event in events:
            kind = event.get('type')
            if kind == 'key_down':
                key = event['keycode']
                if key in self.held_keys:
                    continue
                self.held_keys.add(key)
            elif kind == 'key_up':
                key = event['keycode']
                if key not in self.held_keys:
                    issues.append({'type': 'key_up_without_down', 'keycode': key})
                    continue
                self.held_keys.remove(key)
            emitted.append(event)
        return emitted, issues


def trace(decisions, duration_frames, adapter=None, *, max_frames=600):
    from .adapters import resolve
    adapter = resolve(adapter=adapter)
    if (type(max_frames) is not int or max_frames < 1 or
            type(duration_frames) is not int or not 1 <= duration_frames <= max_frames):
        raise ValueError('Trace duration exceeds the episode frame limit')
    events = []
    previous = -1
    for d in decisions:
        frame = d['frame']
        if type(frame) is not int or not previous < frame < duration_frames:
            raise ValueError('Decision frames must increase and precede trace end')
        previous = frame
        # A rollout replays exactly the events it sent to Godot. Callers that
        # construct a trace directly retain the original adapter mapping.
        emitted = d['emitted_events'] if 'emitted_events' in d else adapter.events(d)
        events.extend({'frame': frame, **event} for event in emitted)
    return {'scenario': adapter.default_scenario, 'duration_frames': duration_frames, 'events': events}


def gain(before, after, minimum_seconds=.25, adapter=None):
    """Require comparable verified episodes, then use the task objective."""
    if not before.get('replay_verified') or not after.get('replay_verified'):
        return False
    if any(before.get(k) != after.get(k) for k in ('project_sha256', 'scenario', 'seed', 'decision_frames', 'task_id', 'player_state_mode')):
        return False
    from .adapters import resolve
    return resolve(adapter=adapter).improved(before['replay'], after['replay'], minimum_seconds)


def train(skill, output, rollout, reflect, rounds=1, *, adapter=None):
    if not 1 <= rounds <= 5:
        raise ValueError('rounds must be 1–5 for this adapter')
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    (out / 'initial_skill.txt').write_text(skill)
    current = rollout(skill, out / 'baseline')
    history = []
    if not current.get('replay_verified'):
        raise RuntimeError('Baseline step/replay mismatch; inspect baseline before training')
    for i in range(rounds):
        d = out / f'round-{i+1:03d}'
        d.mkdir()
        proposal = reflect(skill, current, history)
        candidate = proposal['skill'].strip()
        if not candidate:
            raise ValueError('Candidate skill must not be empty')
        write_json(d / 'proposal.json', proposal)
        record = {'round': i+1, 'accepted': False}
        if candidate == skill.strip():
            record['reason'] = 'unchanged skill'
        else:
            result = rollout(candidate, d / 'candidate')
            record.update(before=current['replay'], candidate=result)
            # Re-evaluate BOTH policies before selecting a noisy winner.
            if gain(current, result, adapter=adapter):
                control = rollout(skill, d / 'confirm-current')
                confirm = rollout(candidate, d / 'confirm-candidate')
                record.update(confirm_current=control, confirm_candidate=confirm)
                if gain(control, confirm, adapter=adapter):
                    skill, current = candidate, confirm
                    record['accepted'] = True
            record['reason'] = 'accepted after repeat' if record['accepted'] else 'no confirmed replay improvement'
        history.append(record)
        write_json(d / 'result.json', record)
        write_json(out / 'history.json', history)
        (out / 'best_skill.txt').write_text(skill+'\n')
    result = {'skill': skill, 'best': current, 'history': history}
    write_json(out / 'result.json', result)
    return result
