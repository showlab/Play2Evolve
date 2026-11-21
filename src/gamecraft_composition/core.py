"""Transactional project edits and acceptance gates; standard library only."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path, PurePosixPath

EDITABLE = {'.gd', '.tscn', '.tres'}
SKIP = {'.git', '.godot', '__pycache__'}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def editable(name):
    p = PurePosixPath(name)
    return (not p.is_absolute() and bool(p.parts) and
            all(x not in {'.', '..', 'demo_outputs', 'tests', 'solution'} and not x.startswith('.') for x in p.parts)
            and (name == 'project.godot' or p.suffix in EDITABLE))


def snapshot(source, dest):
    source, dest = Path(source).resolve(), Path(dest).resolve()
    if source == dest or source in dest.parents:
        raise ValueError('Snapshot must be outside the source project')
    # Do not follow links into another project or shared assets while editing.
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('Project symlinks are unsupported; copy required resources first')
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*SKIP))


def sources(project, max_bytes=120_000):
    result = {}
    for p in sorted(Path(project).rglob('*')):
        name = p.relative_to(project).as_posix()
        if p.is_file() and editable(name):
            result[name] = p.read_text()
    if sum(len(x.encode()) for x in result.values()) > max_bytes:
        raise ValueError('Project source exceeds context limit; select a smaller project for this first adapter')
    return result


def apply_patch(project, patch):
    """Exact replacements only. Validate the whole patch before writing any file."""
    edits = patch.get('edits', [])
    if not isinstance(edits, list) or not 1 <= len(edits) <= 8:
        raise ValueError('Expected 1–8 edits')
    pending = {}
    root = Path(project).resolve()
    for e in edits:
        name, old, new = e['path'], e['old'], e['new']
        if not all(isinstance(v, str) for v in (name, old, new)):
            raise ValueError('Patch fields must be strings')
        if not editable(name) or not old or old == new or len(new.encode()) > 40_000:
            raise ValueError(f'Invalid edit: {name}')
        p = root / name
        if root not in p.resolve().parents or any(x.is_symlink() for x in [p, *p.parents]):
            raise ValueError('Edit escapes project or follows symlink')
        if not p.is_file():
            raise ValueError(f'Only existing source files may be edited: {name}')
        text = pending.get(p, p.read_text())
        if text.count(old) != 1:
            raise ValueError(f'Edit must match exactly once: {name}')
        pending[p] = text.replace(old, new, 1)
    for p, text in pending.items():
        p.write_text(text)


def protected_digest(project):
    """Assets, demos, and other non-editable resources must stay identical."""
    result = {}
    for p in Path(project).rglob('*'):
        rel = p.relative_to(project)
        if any(x in SKIP for x in rel.parts) or p.suffix in {'.uid', '.import'}:
            continue
        if p.is_symlink():
            raise ValueError('Project gained a symlink')
        if p.is_file() and not editable(rel.as_posix()):
            result[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def accept(before, after, feedback):
    ids = [x['id'] for x in feedback['criteria']]
    for evaluation in (before, after):
        if not evaluation.get('build_ok') or evaluation.get('errors') or evaluation.get('num_demos', 0) < 1:
            return False, 'build, replay, or judge failed'
        values = evaluation.get('scores', {})
        if any(k not in values or not isinstance(values[k], (int, float)) or
               not math.isfinite(values[k]) or not 0 <= values[k] <= 1 for k in ids):
            return False, 'missing or invalid scores'
    target = feedback['target']
    target_gain = after['scores'][target] - before['scores'][target]
    repair_criteria = feedback.get('repair_criteria', [])
    repair_gain = feedback.get('repair_min_gain', 0.05)
    repaired = [key for key in repair_criteria
                if after['scores'][key] - before['scores'][key] >= repair_gain]
    if target_gain < feedback.get('min_gain', 0.05) and not repaired:
        return False, 'neither aesthetic target nor a defect criterion improved sufficiently'
    tolerance = feedback.get('regression_tolerance', 0.02)
    if any(after['scores'][k] < before['scores'][k] - tolerance for k in ids if k != target):
        return False, 'another criterion regressed'
    if repaired:
        return True, 'improved defect criteria: ' + ', '.join(repaired)
    return True, 'aesthetic target improved without measured regression'


def accept_targeted(before, after, feedback, diagnosis, verification):
    """Keep evidenced progress without claiming an unfinished repair is complete."""
    ids = [x['id'] for x in feedback['criteria']]
    for evaluation in (before, after):
        if not evaluation.get('build_ok') or evaluation.get('errors') or evaluation.get('num_demos', 0) < 1:
            return False, 'build, replay, or judge failed'
        values=evaluation.get('scores',{})
        if any(k not in values or not isinstance(values[k],(int,float)) or
               not math.isfinite(values[k]) or not 0<=values[k]<=1 for k in ids):
            return False, 'missing or invalid scores'
    from gamecraft_training.package.repair_verifier import state
    status=state(verification)
    if status=='not_exercised' or verification.get('coverage_passed') is False:
        return False, 'targeted repair was not exercised: '+verification.get('coverage_reason','')
    if verification.get('target_change')=='worse':
        return False, 'targeted defect visibly worsened'
    if any(r.get('severity','critical')=='critical' for r in verification.get('regressions',[])):
        return False, 'targeted verifier found a critical regression'
    if (status=='gone' and verification.get('defect_fixed') and
            verification.get('target_change','better')=='better'):
        return True, 'targeted defect is gone without a critical regression'
    old,new=verification.get('before_severity'),verification.get('after_severity')
    if (verification.get('target_change')=='better' and
            all(isinstance(x,(int,float)) and math.isfinite(x) for x in (old,new)) and new<old):
        return True, 'targeted defect visibly improved; remaining work carries forward'
    return False, 'targeted defect did not visibly improve'


def validate_feedback(f):
    ids = [c['id'] for c in f['criteria']]
    if not ids or len(set(ids)) != len(ids) or any(not x.isidentifier() or not x.isascii() or x == 'BUILD' for x in ids):
        raise ValueError('Criteria require unique ASCII identifiers other than BUILD')
    if f['target'] not in ids or not f.get('problem') or any(not c.get('description') for c in f['criteria']):
        raise ValueError('Feedback needs problem, target, and criterion descriptions')
    for key, default in [('min_gain', .05), ('regression_tolerance', .02)]:
        value = f.get(key, default)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f'Invalid {key}')
    if f.get('min_gain', .05) <= 0:
        raise ValueError('min_gain must be positive')


def optimize(project, output, feedback, propose, evaluate, steps=1):
    """Callbacks make LLM/Godot optional for deterministic tests."""
    validate_feedback(feedback)
    project, output = Path(project).resolve(), Path(output).resolve()
    if project == output or project in output.parents:
        raise ValueError('Output must be outside the input project')
    if steps < 1 or steps > 20:
        raise ValueError('steps must be between 1 and 20')
    if not (project / 'project.godot').is_file():
        raise ValueError('Expected an existing Godot project')
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'feedback.json', feedback)
    baseline = output / 'baseline'
    snapshot(project, baseline)
    protected = protected_digest(baseline)
    current = evaluate(baseline, output / 'baseline-evaluation', feedback)
    if protected_digest(baseline) != protected:
        raise ValueError('Baseline execution changed protected resources')
    ok, reason = accept(current, current, {**feedback, 'min_gain': 0})
    if not ok:
        write_json(output / 'baseline-result.json', current)
        raise ValueError('Baseline must build, replay, and judge successfully: ' + reason)
    best, history = baseline, []
    for step in range(1, steps + 1):
        trial = output / f'step-{step:03d}'
        trial.mkdir()
        record = {'step': step, 'before': current, 'accepted': False}
        try:
            patch = propose(best, feedback, current, history)
            write_json(trial / 'patch.json', patch)
            candidate = trial / 'project'
            snapshot(best, candidate)
            apply_patch(candidate, patch)
            if protected_digest(candidate) != protected:
                raise ValueError('Patch changed protected resources')
            after = evaluate(candidate, trial / 'evaluation', feedback)
            if protected_digest(candidate) != protected:
                raise ValueError('Candidate execution changed assets or input traces')
            record['after'] = after
            record['accepted'], record['reason'] = accept(current, after, feedback)
            if record['accepted']:
                best, current = candidate, after
        except Exception as exc:
            record['error'] = True
            record['reason'] = f'{type(exc).__name__}: {exc}'
        history.append(record)
        write_json(trial / 'result.json', record)
        write_json(output / 'history.json', history)
    snapshot(best, output / 'accepted-project')
    result = {'accepted_project': str(output / 'accepted-project'), 'evaluation': current,
              'accepted_steps': sum(r['accepted'] for r in history),
              'failed_steps': sum(bool(r.get('error')) for r in history), 'history': history}
    write_json(output / 'result.json', result)
    return result
