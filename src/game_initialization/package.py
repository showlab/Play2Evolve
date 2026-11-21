"""Freeze initialized projects; load isolated run copies without regenerating games.

Packages contain trusted local Python/GDScript. Hashes protect experiment
identity and detect accidental edits; they are not a sandbox or signature.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import uuid
from pathlib import Path

from gamecraft_behavior.adapters import resolve
from gamecraft_behavior.core import digest, write_json

SKIP = {'.godot', '__pycache__', '.git'}
REQUIRED = ('game/project.godot', 'game/game_plan.json', 'adapter/__init__.py',
            'asset_plan.json', 'composition_spec.json', 'scenarios.json', 'initialization.json')


def inventory(root):
    root = Path(root).resolve()
    files = {}
    for p in root.rglob('*'):
        rel = p.relative_to(root)
        if any(v in SKIP for v in rel.parts):
            continue
        if p.is_symlink():
            raise ValueError(f'Package symlinks are unsupported: {rel}')
        if p.is_file() and rel.as_posix() != 'initialization.json':
            files[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return files


def manifest_hash(metadata):
    return hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()


def load_adapter(root):
    """Execute a trusted package's local adapter factory (validate first)."""
    root = Path(root).resolve()
    name = '_game_package_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, root/'adapter/__init__.py',
                                                  submodule_search_locations=[str(root/'adapter')])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        adapter = resolve(root/'game', module.create_adapter())
    except Exception:
        for key in list(sys.modules):
            if key == name or key.startswith(name+'.'):
                del sys.modules[key]
        raise
    bridge = adapter.bridge_script.resolve()
    if root/'adapter' not in bridge.parents or not bridge.is_file():
        raise ValueError('Adapter bridge must be a file inside this package adapter directory')
    return adapter


def inspect_package(root, *, sealed=True):
    root = Path(root).resolve()
    files = inventory(root)  # Reject links before importing any package code.
    for name in REQUIRED:
        if not (root/name).is_file():
            raise ValueError(f'Missing package file: {name}')
    meta = json.loads((root/'initialization.json').read_text())
    if meta.get('schema_version') != 1 or not re.fullmatch(r'[a-zA-Z0-9_.-]+', meta.get('version', '')):
        raise ValueError('Expected schema_version=1 and a nonempty path-safe version')
    task = meta.get('task_id')
    if not isinstance(task, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', task):
        raise ValueError('Expected a path-safe task_id')
    for name in ['game/game_plan.json', 'asset_plan.json']:
        if json.loads((root/name).read_text()).get('task_id') != task:
            raise ValueError(f'Task mismatch in {name}')
    asset_plan = json.loads((root/'asset_plan.json').read_text())
    composition = json.loads((root/'composition_spec.json').read_text())
    if not isinstance(asset_plan.get('assets'), list) or not isinstance(composition.get('mutable'), dict):
        raise ValueError('Expected asset list and explicit mutable composition parameters')
    if sealed:
        if meta.get('state') != 'frozen' or meta.get('files') != files:
            raise ValueError('Package is not frozen or file hashes changed')
        check = root/'checks/report.json'
        if not check.is_file():
            raise ValueError('Package lacks initialization check status')
        report = json.loads(check.read_text())
        if (report.get('passed') is not True and not
                (meta.get('verification') == 'structural_only' and
                 report.get('status') == 'not_run')):
            raise ValueError('Package lacks valid initialization check status')
    adapter = load_adapter(root)
    if adapter.task_id != task:
        raise ValueError('Adapter task mismatch')
    cases = json.loads((root/'scenarios.json').read_text())
    if not isinstance(cases, list) or not cases:
        raise ValueError('At least one deterministic initialization scenario is required')
    ids = set()
    for case in cases:
        ident = case['id']
        adapter.validate_scenario(ident)
        if ident in ids:
            raise ValueError('Scenario IDs must be unique')
        ids.add(ident)
        if case.get('purpose') not in ('initialization', 'train', 'validation', 'confirmation', 'test'):
            raise ValueError('Each scenario needs a declared purpose')
        if case.get('launch', 'scenario') not in ('scenario', 'default'):
            raise ValueError('Scenario launch must be scenario or default')
        frames = case.get('step_frames', 6)
        actions = case.get('actions')
        if (type(frames) is not int or not 1 <= frames <= 30 or not isinstance(actions, list)
                or not actions or frames*len(actions) > 600):
            raise ValueError('Scenario actions must fit the 600-frame replay budget')
        for a in actions:
            adapter.scripted_action(a)
        for check in case.get('control_checks', []):
            if (not isinstance(check, dict) or set(check) != {'action', 'field', 'op'}
                    or check['action'] not in adapter.allowed_actions
                    or check['field'] not in adapter.replay_fields
                    or check['op'] not in ('increase', 'decrease', 'change')):
                raise ValueError('Invalid observable control check')
    if not any(c['purpose'] == 'initialization' for c in cases):
        raise ValueError('Need an initialization scenario; held-out scenarios are not run during initialization')
    if adapter.pointer_actions and not any(
            isinstance(action, dict) and action.get('action') in adapter.pointer_actions
            for case in cases if case['purpose'] == 'initialization' for action in case['actions']):
        raise ValueError('Pointer-enabled game needs an initialization scenario using its pointer control')
    if meta.get('control_contract_version', 0) >= 1 and adapter.pointer_actions and not any(
            check['action'] in adapter.pointer_actions
            for case in cases if case['purpose'] == 'initialization'
            for check in case.get('control_checks', [])):
        raise ValueError('Pointer-enabled game needs an observable pointer control check')
    return meta, adapter, cases


def separate(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if (destination.exists() or source == destination or source in destination.parents
            or destination in source.parents):
        raise ValueError('Destination must be new and separate from source')
    return source, destination


def check_control_effects(result, checks):
    """Require at least one observed state change immediately after each named input."""
    decisions = result.get('decisions', [])
    final = result.get('stepped', {})
    rows = []
    for check in checks:
        passed = False
        for index, decision in enumerate(decisions):
            if decision['action'] != check['action']:
                continue
            before = decision.get('evaluation_state', {})
            after = (decisions[index + 1].get('evaluation_state', {})
                     if index + 1 < len(decisions) else final)
            old, new = before.get(check['field']), after.get(check['field'])
            if check['op'] == 'change':
                passed = old is not None and new is not None and old != new
            elif (type(old) in (int, float) and type(new) in (int, float)):
                passed = new > old if check['op'] == 'increase' else new < old
            if passed:
                break
        rows.append({**check, 'passed': passed})
    return rows


def freeze(draft, output, godot, *, scenario_ids=None):
    """Replay selected initialization scenarios on a copy, then seal it.

    The default checks every initialization scenario. Bootstrap may request a
    small smoke subset; later initialization validation checks the full set.
    """
    from gamecraft_behavior.runtime import rollout
    draft, output = separate(draft, output)
    meta, _, _ = inspect_package(draft, sealed=False)
    if meta.get('state') != 'draft':
        raise ValueError('Freeze expects a draft; checkout frozen versions for runs')
    shutil.copytree(draft, output, ignore=shutil.ignore_patterns(*SKIP))
    meta, adapter, cases = inspect_package(output, sealed=False)
    checks = output/'checks'
    checks.mkdir(exist_ok=False)
    before = digest(output/'game')
    initialization_ids = [case['id'] for case in cases if case['purpose'] == 'initialization']
    selected = set(initialization_ids if scenario_ids is None else scenario_ids)
    if not selected or not selected.issubset(initialization_ids):
        raise ValueError('Freeze needs valid initialization scenario IDs')
    reports = []
    try:
        for i, case in enumerate(cases):
            if case['id'] not in selected:
                continue
            sequence = iter(case['actions'])
            def decide(image, skill, recent, frames, **kwargs):
                return adapter.scripted_action(next(sequence), 'Declared initialization check')
            result = rollout(output/'game', checks/f'case-{i:03}', '', decide,
                             godot=str(godot), adapter=adapter, scenario=case['id'],
                             normal_launch=case.get('launch', 'scenario') == 'default',
                             step_frames=case.get('step_frames', 6), max_decisions=len(case['actions']),
                             player_state='engine')
            control_results = check_control_effects(result, case.get('control_checks', []))
            reports.append({'scenario': case['id'], 'replay_verified': result['replay_verified'],
                            'launch': case.get('launch', 'scenario'),
                            'decisions': len(result['decisions']), 'control_checks': control_results})
            if not result['replay_verified']:
                raise RuntimeError(f'Initialization replay mismatch: {case["id"]}')
            if any(not row['passed'] for row in control_results):
                raise RuntimeError(f'Initialization control did not produce its declared effect: {case["id"]}')
        if digest(output/'game') != before:
            raise RuntimeError('Initialization changed the source game')
        write_json(checks/'report.json', {'passed': True, 'cases': reports,
                   'replay_scope': 'all_initialization' if scenario_ids is None else 'bootstrap_smoke',
                   'unchecked_initialization_scenarios': [ident for ident in initialization_ids if ident not in selected],
                   'scope': 'scripted actions, observations and deterministic replay for listed cases only; not full requirement, visual-quality or model-policy evaluation'})
        meta.update(state='frozen', files=inventory(output))
        write_json(output/'initialization.json', meta)
        inspect_package(output)
    except Exception as exc:
        write_json(checks/'failure.json', {'error': f'{type(exc).__name__}: {exc}'})
        raise
    return output


def seal_without_replay(draft, output):
    """Package an optimized draft without a post-optimization playtest."""
    draft, output = separate(draft, output)
    meta, _, _ = inspect_package(draft, sealed=False)
    if meta.get('state') != 'draft':
        raise ValueError('Sealing expects a draft')
    shutil.copytree(draft, output, ignore=shutil.ignore_patterns(*SKIP))
    (output/'checks').mkdir(exist_ok=False)
    write_json(output/'checks/report.json', {
        'status': 'not_run', 'passed': None,
        'scope': 'Final optimized project was packaged without post-optimization replay or quality acceptance.'})
    meta.update(state='frozen', verification='structural_only', files=inventory(output))
    write_json(output/'initialization.json', meta)
    inspect_package(output)
    return output


def checkout(package, output):
    """Verify identity before copying or executing adapter code for a new run."""
    package, output = separate(package, output)
    meta, _, _ = inspect_package(package)
    output.mkdir(parents=True)
    target = output/'initial'
    shutil.copytree(package, target, ignore=shutil.ignore_patterns(*SKIP))
    _, adapter, cases = inspect_package(target)
    write_json(output/'parent.json', {'source': str(package), 'task_id': meta['task_id'],
               'version': meta['version'], 'manifest_sha256': manifest_hash(meta)})
    return target/'game', adapter, cases
