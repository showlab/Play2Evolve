"""Post-RSI coverage collection on the selected game; no training or game edits."""
from __future__ import annotations

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import difflib
from functools import partial
import json
import os
from pathlib import Path
from play2evolve.runtime import configure_benchmark
import shutil
import subprocess

from codex_provider import environment_values, from_environment, validate as validate_provider
from game_initialization.package import load_adapter
from gamecraft_behavior.core import digest, write_json
from gamecraft_training.package import final_showcase
from gamecraft_training.package import models
from gamecraft_training.package.upgrade import with_generic_pointer_actions
from gamecraft_composition.core import sources
from . import rsi_baseline_comparison
from .packaging import validate_trace
from .trained_demos import collect_official, file_hash, public_trace
from .showcase_coverage import CoverageIncompleteError, coverage_summary, run_incremental


def read(path):
    return json.loads(Path(path).read_text())


def run_path(run_dir, value, recorded_root):
    """Rebase run-owned paths when the same run is mounted at a different root."""
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(recorded_root)
        except ValueError as exc:
            raise ValueError(f'Artifact is outside the recorded RSI run: {value}') from exc
    resolved = (run_dir / path).resolve()
    if resolved == run_dir or run_dir not in resolved.parents:
        raise ValueError(f'Artifact escapes the RSI run: {value}')
    return resolved


def load_source(run_dir, *, use_working_project=False):
    """Select the accepted output, including successful runs with recovered errors."""
    run_dir = Path(run_dir).resolve()
    info, config = read(run_dir / 'result.json'), read(run_dir / 'run.json')
    if (info.get('profile') != 'rsi' or config.get('profile') != 'rsi'
            or info.get('state') not in ('completed', 'completed_with_errors')):
        raise ValueError('Expected a finished RSI run')
    status = info.get('upgrade_status')
    if status not in ('accepted', 'retained_original'):
        raise ValueError('RSI must have a completed final selection')
    recorded_root = Path(info['final_review']).parent
    review_path = run_path(run_dir, info['final_review'], recorded_root)
    review = read(review_path)
    recorded_selected = 'upgrade' if status == 'accepted' else 'original'
    if review.get('status') != status or review.get('selected') != recorded_selected:
        raise ValueError('RSI result and final review disagree')
    recorded_project = run_path(run_dir, info['project'], recorded_root)
    expected = info['working_project'] if recorded_selected == 'upgrade' else info['original_project']
    if recorded_project != run_path(run_dir, expected, recorded_root):
        raise ValueError('RSI project does not match the final selected version')
    selected = recorded_selected
    project = recorded_project
    if use_working_project:
        history = read(run_dir / 'history.json')
        working = run_path(run_dir, info['working_project'], recorded_root)
        if (not history or
                run_path(run_dir, history[-1]['project_after'], recorded_root) != working or
                not any(row.get('action') == 'accept_repair' or
                        row.get('status') == 'provisional' for row in history) or
                info.get('full_build_status', 'playable') != 'playable'):
            raise ValueError('RSI working project was not admitted by round review')
        project = working
        selected = 'upgrade'
    if selected == 'upgrade' and info.get('full_build_status', 'playable') != 'playable':
        raise ValueError('Selected upgrade was not marked playable')
    skill_path = info['working_skill'] if use_working_project else info['skill']
    skill = run_path(run_dir, skill_path, recorded_root)
    if not (project / 'project.godot').is_file() or not skill.is_file():
        raise ValueError('Final selected game or Behavior Skill is missing')
    if (any(p.is_symlink() for p in project.rglob('*'))
            or 'BehaviorBridge=' in (project / 'project.godot').read_text()
            or (project / 'behavior_bridge.gd').exists()):
        raise ValueError('Expected a clean game without symlinks or a development bridge')
    if read(project / 'game_plan.json')['task_id'] != config['task_id']:
        raise ValueError('Selected game and RSI task differ')
    initial = run_dir / 'initial'
    requirements = read(initial / 'requirements.json')['requirements']
    scenarios = read(initial / 'scenarios.json')
    goals = read(run_path(run_dir, info['upgrade_plan'], recorded_root))
    tasks = read(run_dir / 'rsi_upgrade_playtests.json')
    tracked = [run_dir / 'result.json', run_dir / 'run.json', review_path, skill,
               initial / 'requirements.json', initial / 'scenarios.json',
               run_path(run_dir, info['upgrade_plan'], recorded_root),
               run_dir / 'rsi_upgrade_playtests.json']
    if use_working_project:
        tracked.append(run_dir / 'history.json')
    return {'run': run_dir, 'info': info, 'config': config, 'project': project,
            'skill_path': skill, 'skill': skill.read_text().strip(),
            'requirements': requirements, 'scenarios': scenarios,
            'goal': goals, 'tasks': tasks, 'selected': selected,
            'selection_override': 'working_project' if use_working_project else None,
            'metadata_hashes': {str(path): file_hash(path) for path in tracked},
            'adapter_hashes': digest(initial / 'adapter')}


def coverage_inputs(source, step_frames):
    """Public goals only. Upgrade requirements are never inferred from a rubric."""
    public = source['requirements']
    public = [r for r in public if r.get('dynamic')] or public
    requirements = [{**r, 'dynamic': True, 'source': 'public_requirement'} for r in public]
    ids = {row['id'] for row in requirements}
    if not ids or len(ids) != len(requirements):
        raise ValueError('Public requirements must have distinct IDs')
    cases = {row['id']: row for row in source['scenarios']}
    normal = next((row for row in cases.values() if row.get('launch') == 'default'), None)
    if normal is None:
        raise ValueError('Final showcase needs an existing normal-launch declaration')
    by_profile = {row['profile']: row for row in source['tasks']}
    profiles = []

    def add_requirement(name, description, verification):
        ident = f'RSI_{name}'
        while ident in ids:
            ident += '_'
        ids.add(ident)
        requirements.append({'id': ident, 'description': description,
                             'verification': verification, 'dynamic': True,
                             'source': 'rsi_goal'})
        return ident

    def add_profile(name, task, assigned, expected, retry_ids):
        # RSI may name adapter.default_scenario (e.g. "ready") for a normal
        # launch, while scenarios.json names that same launch "default_flow".
        if ((task.get('launch') == 'scenario' and task.get('id') not in cases)
                or task.get('launch') not in ('default', 'scenario')
                or not isinstance(task.get('goal'), str) or not task['goal'].strip()):
            raise ValueError(f'Invalid existing RSI playtest goal: {name}')
        profiles.append({'id': name, 'goal': task['goal'],
                         'scenario': None if task['launch'] == 'default' else task['id'],
                         'requirement_ids': assigned, 'expected': expected,
                         'retry_requirement_ids': retry_ids,
                         'max_decisions': final_showcase.MAX_FRAMES // step_frames})

    core = by_profile['core']
    # On a rejected upgrade, do not ask the original game to demonstrate proposed features.
    if source['selected'] == 'original':
        core = {**core, 'goal': 'Demonstrate the existing core gameplay and its visible '
                'outcomes using the public requirements of the selected original game.'}
    core_id = add_requirement('CORE', core['goal'], 'Show actual legal play and its visible outcome.')
    public_ids = [row['id'] for row in public]
    add_profile('rsi_core', core, [*public_ids, core_id], core['goal'], [core_id])
    if source['selected'] == 'upgrade':
        goal = source['goal']
        upgrade_ids = []
        for key, label in [('gameplay_plan', 'GAMEPLAY'), ('aesthetic_plan', 'AESTHETIC')]:
            track = goal.get(key) or {}
            change = track.get('change')
            if not isinstance(change, str) or not change.strip():
                raise ValueError(f'RSI showcase needs a saved {key}.change')
            upgrade_ids.append(add_requirement(label, change, track.get('validation', change)))
        task = {**by_profile['upgrade'], 'goal': by_profile['upgrade']['goal'] +
                ' Also show the upgraded presentation: ' + goal['aesthetic_plan']['change']}
        add_profile('rsi_upgrade', task, [*public_ids, *upgrade_ids],
                    task['goal'] + ' Show the planned gameplay change and its visual presentation.',
                    upgrade_ids)
    # Most RSI core tasks start inside gameplay; normal menu entry still needs evidence.
    if not any(row['scenario'] is None for row in profiles):
        entry = {'id': normal['id'], 'launch': 'default',
                 'goal': 'Show the normal title/menu entry and enter playable core content '
                         'through the existing controls; allow visible transitions to finish.'}
        entry_id = add_requirement('ENTRY', entry['goal'], 'Title/menu followed by playable content.')
        add_profile('rsi_entry', entry, [entry_id], entry['goal'], [entry_id])
    proposal = {'profiles': profiles}
    final_showcase.validate_plan(proposal, source['scenarios'], requirements, step_frames)
    return requirements, proposal


def assess_requirements(row, official, **kwargs):
    """Keep each coverage response bounded even for games with 30+ public goals."""
    ids = row['requirement_ids']
    coverage = []
    for start in range(0, len(ids), 8):
        chunk = {**row, 'requirement_ids': ids[start:start + 8]}
        coverage.extend(final_showcase.assess(chunk, official, **kwargs))
    return coverage


def reusable_demos(source, demo_dir=None):
    """Validated initialization inputs, not evidence about the RSI game."""
    cases = {row['id'] for row in source['scenarios']}
    root = Path(demo_dir).resolve() if demo_dir is not None else source['run'] / 'initial/game/demo_outputs'
    demos = []
    if root.is_dir():
        for path in sorted(root.glob('*.json')):
            if path.is_symlink():
                raise ValueError(f'Initialization trace is a symlink: {path}')
            trace = validate_trace(path)
            if trace.get('scenario') is not None and trace['scenario'] not in cases:
                raise ValueError(f'Initialization trace names an undeclared scenario: {path}')
            demos.append({'file': path.name, 'path': path,
                          'scenario': trace.get('scenario'),
                          'duration_frames': trace['duration_frames'],
                          'events': len(trace['events']), 'sha256': file_hash(path)})
    return demos


def reusable_rsi_playtests(source, output, project_hash):
    """Export RSI playtest inputs recorded on the exact selected game version.

    A round's before-playtest belongs to the pre-repair game. A candidate
    playtest is eligible only when its recorded project hash matches the final
    selected project. The exported inputs still need official OS-input replay.
    """
    if source['selected'] != 'upgrade':
        return []
    run_dir = source['run']
    recorded_root = Path(source['info']['final_review']).parent
    cases = {row['id'] for row in source['scenarios']}
    candidates = []
    groups = [(0, run_dir.glob('full_build/attempt-*/playtest/episode-*/result.json'))]
    groups.append((1, run_dir.glob('round-*/playtest/episode-*/result.json')))
    groups.append((2, run_dir.glob('round-*/candidate_playtest/episode-*/result.json')))
    groups.append((3, run_dir.glob('round-*/attempt-*/playtest/episode-*/result.json')))
    for stage, paths in groups:
        for path in sorted(paths):
            record = read(path)
            profile = record.get('player_profile')
            if (profile not in ('core', 'upgrade') or not record.get('replay_verified')
                    or record.get('project_sha256') != project_hash):
                continue
            normal = record.get('normal_launch') is True
            scenario = record.get('scenario')
            if not normal and scenario not in cases:
                continue
            try:
                trace = run_path(run_dir, record['trace'], recorded_root)
                exported = public_trace({**record, 'trace': str(trace)})
            except (KeyError, ValueError, OSError):
                continue
            candidates.append((path, stage, profile, trace, exported, normal, scenario))
    # Prefer the most recent playtest of each goal, while admitting only traces
    # observed on exactly the final game. This keeps the reusable set small.
    chosen = {}
    for item in candidates:
        path, stage, profile = item[:3]
        round_name = next((part for part in path.parts if part.startswith('round-')), '')
        round_number = int(round_name[6:]) if round_name[6:].isdigit() else 0
        attempt_name = next((part for part in path.parts if part.startswith('attempt-')), '')
        attempt_number = int(attempt_name[8:]) if attempt_name[8:].isdigit() else 0
        rank = (round_number, stage == 2, attempt_number, str(path))
        if profile not in chosen or rank > chosen[profile][0]:
            chosen[profile] = (rank, item)
    if not chosen:
        return []
    directory = output / 'reusable_rsi_playtests'
    directory.mkdir(parents=True)
    demos = []
    for profile in ('core', 'upgrade'):
        if profile not in chosen:
            continue
        path, _, _, trace, exported, normal, scenario = chosen[profile][1]
        dest = directory / f'{profile}.json'
        write_json(dest, exported)
        validate_trace(dest)
        demos.append({'file': dest.name, 'path': dest,
                      'scenario': None if normal else scenario,
                      'duration_frames': exported['duration_frames'],
                      'events': len(exported['events']), 'sha256': file_hash(dest),
                      'source': 'rsi_playtest_trace',
                      'source_file': str(trace), 'source_sha256': file_hash(trace),
                      'source_result': str(path), 'source_result_sha256': file_hash(path)})
    return demos


def changed_game_summary(source):
    """Give the planner concrete change evidence without sending video or secret rubric."""
    before = source['run'] / 'initial/game'
    after = source['project']
    old, new = digest(before), digest(after)
    changed = sorted(name for name in old.keys() | new.keys()
                     if old.get(name) != new.get(name) and
                     not name.startswith('demo_outputs/'))
    diffs = []
    for name in changed:
        if not name.endswith(('.gd', '.tscn', '.tres', '.godot')):
            continue
        left, right = before / name, after / name
        a = left.read_text(errors='replace').splitlines() if left.is_file() else []
        b = right.read_text(errors='replace').splitlines() if right.is_file() else []
        diffs.extend(difflib.unified_diff(a, b, fromfile=f'old/{name}',
                                          tofile=f'new/{name}', lineterm=''))
        if sum(map(len, diffs)) > 24000:
            break
    return {'changed_files': changed[:100], 'source_diff': '\n'.join(diffs)[:24000]}


def plan_route_merges(project, routes, requirements, choices, missing, *, backend, model):
    """Assign RSI goals to existing initialization routes, using public evidence only."""
    goals = [r for r in requirements if r['id'] in missing]
    schema = {'assignments': {'type': 'array', 'items': {'type': 'object',
              'additionalProperties': False, 'required': ['source_file', 'goal_ids', 'goal'],
              'properties': {'source_file': {'type': 'string'},
                             'goal_ids': {'type': 'array', 'minItems': 1,
                                          'items': {'type': 'string'}},
                             'goal': {'type': 'string'}}}}}
    return models.call(
        'Merge the missing RSI showcase goals into EXISTING initialization routes. '
        'Return one assignment per route you need to rewrite, assigning each missing RSI '
        'goal exactly once. Keep each route\'s original scenario and visible gameplay '
        'outcomes. Select a route whose legal interactions can show both its old content '
        'and the new goal within 20 seconds; do not concatenate input scripts or invent '
        'a new scenario. Each assignment is ONE launch and ONE route; never ask the '
        'Player to record or compare another route inside it. The Player will author '
        'a reactive program for each assignment. '
        'Use only public requirements, recorded route observations, and game source. '
        'Do not use hidden benchmark criteria or claim an unobserved result.',
        {'routes': [{'file': d['file'], 'scenario': d['scenario'],
                    'duration_frames': d['duration_frames'], 'event_count': d['events'],
                    'input_events_sample': read(d['path'])['events'][:40],
                    'observed_public_coverage': [x for x in c['coverage']
                        if x['id'] in {r['id'] for r in requirements
                                       if r['source'] == 'public_requirement'}]}
                   for d, c in zip(routes, choices)],
         'missing_rsi_goals': [{'id': r['id'], 'description': r['description'],
                               'verification': r.get('verification', '')} for r in goals],
         'game_source': sources(project),
         'limits': {'max_routes': len(routes), 'frames_per_route': final_showcase.MAX_FRAMES}},
        [], 'rsi_merged_showcase_plan', schema, tokens=4500,
        backend=backend, model=model)


@contextmanager
def environment(values):
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run(*, run_dir, output, bench, godot, backend=None, model=None,
        step_frames=None, provider=None, reasoning_effort=None,
        mode='incremental', showcase_parallelism=5, use_working_project=False,
        initialization_demos=None, initialization_project=None):
    run_dir, output, bench, godot = (Path(p).resolve() for p in (run_dir, output, bench, godot))
    if (output.exists() or output == run_dir or run_dir in output.parents
            or output in run_dir.parents or output == bench or bench in output.parents
            or output in bench.parents):
        raise ValueError('Output must be new and separate from the RSI run and benchmark')
    source = load_source(run_dir, use_working_project=use_working_project)
    if (initialization_demos is None) != (initialization_project is None):
        raise ValueError('Initialization demos and their source project must be provided together')
    if initialization_demos is not None:
        initialization_demos = Path(initialization_demos).resolve()
        initialization_project = Path(initialization_project).resolve()
        if (not initialization_demos.is_dir() or
                not (initialization_project / 'project.godot').is_file() or
                initialization_demos != (initialization_project / 'demo_outputs').resolve()):
            raise ValueError('Explicit initialization demos must belong to their source project')
    if mode not in ('incremental', 'full', 'score_only', 'merged', 'preserve'):
        raise ValueError('Unknown showcase mode')
    config, info = source['config'], source['info']
    settings = config.get('player') or {}
    backend, model = backend or settings.get('backend', 'codex'), model or settings.get('model', 'gpt-6-sol')
    if backend not in ('codex', 'api', 'local') or not model or (backend != 'local' and model == 'local'):
        raise ValueError('An explicit model is required for a nonlocal Player')
    step_frames = config.get('step_frames', 6) if step_frames is None else step_frames
    if type(step_frames) is not int or not 1 <= step_frames <= 30:
        raise ValueError('step_frames must be between 1 and 30')
    if not godot.is_file() or not os.access(godot, os.X_OK):
        raise ValueError('Godot executable is missing')
    if not (bench / 'tasks' / config['task_id'] / 'tests/rubric.json').is_file():
        raise ValueError('Benchmark task is missing')
    # final_showcase uses the workspace checkout for OS replay. Do not silently
    # mix one replay implementation with a different final collection checkout.
    configure_benchmark(bench)
    if mode == 'score_only':
        from .rsi_score_only import run_score_only
        return run_score_only(source, output, bench, godot)
    requirements, initial_profiles = coverage_inputs(source, step_frames)
    adapter = with_generic_pointer_actions(load_adapter(run_dir / 'initial'))
    if adapter.task_id != config['task_id']:
        raise ValueError('Adapter and RSI task differ')
    provider = validate_provider(provider or from_environment())
    project = source['project']
    fingerprint = digest(project)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'status.json', {'state': 'recording'})
    write_json(output / 'configuration.json', {
        'protocol': 'post-rsi-showcase-v2', 'run': str(run_dir),
        'task_id': config['task_id'], 'selected': source['selected'],
        'selection_override': source['selection_override'],
        'initialization_demos': str(initialization_demos) if initialization_demos else None,
        'initialization_project': str(initialization_project) if initialization_project else None,
        'source_state': info['state'], 'source_errors': info.get('errors', []),
        'project_source': str(project), 'project_hashes': fingerprint,
        'input_metadata_hashes': source['metadata_hashes'],
        'adapter_hashes': source['adapter_hashes'],
        'skill_sha256': file_hash(source['skill_path']),
        'player': {'backend': backend, 'model': model, 'mode': 'program'},
        'provider': provider, 'reasoning_effort': reasoning_effort,
        'step_frames': step_frames, 'max_frames_per_demo': final_showcase.MAX_FRAMES,
        'max_demos': final_showcase.MAX_DEMOS,
        'mode': mode,
        'selection': ('Keep each initialization route; compare final-game RSI playtests and repairs by visible evidence'
                      if mode == 'preserve' else
                      'Keep one route per initialization trace and merge RSI goals'
                      if mode == 'merged' else
                      'Check all initialization inputs; preserve observed coverage and repair gaps'
                      if mode == 'incremental' else
                      'Public-goal coverage with at most one policy retry per profile; no score feedback'),
    })
    write_json(output / 'coverage_requirements.json', {'requirements': requirements})
    write_json(output / 'initial_profiles.json', initial_profiles)
    values = {**environment_values(provider), 'LLM_BACKEND': backend,
              'OPTIMIZER_BACKEND': backend, 'GAMECRAFT_BENCH_GODOT_BIN': str(godot)}
    if reasoning_effort:
        values['GAMECRAFT_CODEX_REASONING_EFFORT'] = reasoning_effort
    try:
        frozen = output / 'frozen_project'
        shutil.copytree(project, frozen, ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
        if digest(frozen) != fingerprint:
            raise RuntimeError('Selected game changed while freezing it')
        with environment(values):
            from gamecraft_behavior.program_policy import program_player
            from gamecraft_behavior.runtime import rollout
            retained = reusable_demos(source, initialization_demos) if mode in ('incremental', 'merged', 'preserve') else []
            rsi_playtests = []
            if mode == 'incremental':
                retained += reusable_rsi_playtests(source, output, fingerprint)
            elif mode == 'preserve':
                rsi_playtests = reusable_rsi_playtests(source, output, fingerprint)
            common = dict(adapter=adapter, scenarios=source['scenarios'],
                          requirements=requirements,
                          previous=[{'phase': 'rsi', 'selected': source['selected'],
                                     'goals': initial_profiles['profiles']}],
                          skill=source['skill'], godot=str(godot), model=model,
                          backend=backend, step_frames=step_frames,
                          policy_factory=program_player, rollout=rollout,
                          planner=partial(final_showcase.plan, recording_context='rsi'),
                          assessor=assess_requirements, initial_profiles=initial_profiles)
            if mode == 'preserve':
                from .preserve_showcase import run_preserve
                manifest = run_preserve(
                    frozen, output / 'showcase', demos=retained,
                    baseline_project=initialization_project or source['run'] / 'initial/game',
                    initial_root=source['run'] / 'initial',
                    requirements=requirements, scenarios=source['scenarios'],
                    skill=source['skill'], adapter=adapter, godot=str(godot),
                    model=model, backend=backend, step_frames=step_frames,
                    policy_factory=program_player, rollout=rollout,
                    parallelism=showcase_parallelism, rsi_playtests=rsi_playtests,
                    assessor=assess_requirements,
                    comparator=rsi_baseline_comparison.compare_baseline)
            elif mode == 'merged':
                from .merged_showcase import run_merged
                manifest = run_merged(frozen, output / 'showcase', demos=retained,
                    baseline_project=source['run'] / 'initial/game',
                    initial_root=source['run'] / 'initial',
                    requirements=requirements, scenarios=source['scenarios'],
                    skill=source['skill'], adapter=adapter, godot=str(godot),
                    model=model, backend=backend, step_frames=step_frames,
                    policy_factory=program_player, rollout=rollout,
                    assessor=assess_requirements, planner=plan_route_merges,
                    comparator=rsi_baseline_comparison.compare_baseline)
            elif mode == 'incremental':
                manifest = run_incremental(frozen, output / 'showcase', demos=retained,
                                           baseline_project=source['run'] / 'initial/game', **common)
            else:
                manifest = final_showcase.run(frozen, output / 'showcase', **common)
            submission = output / 'project'
            shutil.copytree(frozen, submission, ignore=shutil.ignore_patterns(
                '.godot', '.git', '__pycache__', 'demo_outputs'))
            demo_dir = submission / 'demo_outputs'
            demo_dir.mkdir()
            entries = []
            selected_entries = {row['name']: row for row in manifest['demos']}
            for path in sorted((output / 'showcase/demo_outputs').glob('*.json')):
                dest = demo_dir / path.name
                shutil.copyfile(path, dest)
                entries.append({'file': dest.name,
                                'source': selected_entries[path.name].get('source', 'fresh_rsi_playtest'),
                                'source_file': str(path), 'sha256': file_hash(dest)})
            traces = list(demo_dir.glob('*.json'))
            if not 1 <= len(traces) <= final_showcase.MAX_DEMOS:
                raise RuntimeError('Final showcase exported an invalid demo count')
            for trace in traces:
                validate_trace(trace)
            write_json(output / 'demo_provenance.json', {'mode': mode, 'demos': entries,
                        'old_videos_reused': False, 'final_game_replay_required': True})
            write_json(output / 'status.json', {'state': 'collecting', 'project': str(submission)})
            # Collection checks BUILD and every exported OS-input trace. It never scores.
            collect_official(submission, output, bench, godot)
            # Inspect the exact final recordings that the scoring process will read.
            # An earlier successful Player/replay is not proof this collection covers it.
            collected = read(output / 'collection/pass-01/breakdown.json')
            final_choices = []
            for demo in collected['demos']:
                row = {'id': demo['demo_id'], 'requirement_ids': [r['id'] for r in requirements],
                       'goal': 'Verify public gameplay and RSI goals in the final scoring evidence.'}
                official = {'frames': demo['frames'], 'duration_seconds': demo['duration_seconds']}
                facts = assess_requirements(row, official, requirements=requirements,
                                            backend=backend, model=model)
                final_choices.append({'row': row, 'coverage': facts,
                                      'official': official})
            final_coverage = coverage_summary(requirements, final_choices)
            route_regressions = []
            if mode == 'preserve':
                from .merged_showcase import compare_scenes
                selected_by_name = {row['name']: row for row in manifest['demos']}
                def check_route(choice):
                    original = selected_by_name[choice['row']['id'] + '.json']
                    if original.get('baseline_scene_status') in ('unverified', 'no_baseline_facts'):
                        return [{'route': original['original_file'],
                                 'requirement_id': 'scene:unverified',
                                 'before': 'unverified', 'after': 'unverified',
                                 'error': 'Original route has no verified baseline scene facts'}]
                    try:
                        scene = compare_scenes(
                            rsi_baseline_comparison.compare_baseline,
                            {'id': 'final_' + choice['row']['id'],
                             'goal': 'Preserve every original visible scene and outcome.'},
                            original['baseline_video'],
                            {**choice['official'], 'video': original['official_video']},
                            output / 'collection/scene_checks' / choice['row']['id'],
                            backend=backend, model=model,
                            facts=original['baseline_scene_facts'])
                    except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        return [{'route': original['original_file'],
                                 'requirement_id': 'scene:unverified',
                                 'before': 'shown', 'after': 'unverified',
                                 'error': str(exc)}]
                    return [{'route': original['original_file'],
                             'requirement_id': 'scene:' + fact['id'],
                             'before': 'shown', 'after': 'not_shown'}
                            for fact in scene['facts'] if not fact['covered']]
                with ThreadPoolExecutor(max_workers=showcase_parallelism) as pool:
                    for problems in pool.map(check_route, final_choices):
                        route_regressions.extend(problems)
            if mode == 'merged':
                rank = {'not_planned': 0, 'not_shown': 0, 'unverified': 0,
                        'partial': 1, 'shown': 2}
                selected_by_name = {row['name']: row for row in manifest['demos']}
                for choice in final_choices:
                    original = selected_by_name[choice['row']['id'] + '.json']
                    if original.get('baseline_scene_status') in ('unverified', 'no_baseline_facts'):
                        route_regressions.append({
                            'route': original['original_file'],
                            'requirement_id': 'scene:unverified',
                            'before': 'unverified', 'after': 'unverified',
                            'error': 'Original route has no verified baseline scene facts'})
                    old = {item['id']: item['status'] for item in
                           original.get('original_coverage', original['coverage'])}
                    new = {item['id']: item['status'] for item in choice['coverage']}
                    for rid in original['protected_public_ids']:
                        if rank.get(new.get(rid), 0) < rank[old[rid]]:
                            route_regressions.append({'route': original['original_file'],
                                                      'requirement_id': rid,
                                                      'before': old[rid],
                                                      'after': new.get(rid)})
                    from .merged_showcase import compare_scenes
                    try:
                        scene = compare_scenes(
                            rsi_baseline_comparison.compare_baseline,
                            {'id': 'final_' + choice['row']['id'],
                             'goal': 'Preserve the original route and its visible outcomes.'},
                            original['baseline_video'],
                            {**choice['official'],
                             'video': original['official_video']},
                            output / 'collection/scene_checks' / choice['row']['id'],
                            backend=backend, model=model,
                            facts=original['baseline_scene_facts'])
                    except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                        route_regressions.append({
                            'route': original['original_file'],
                            'requirement_id': 'scene:unverified',
                            'before': 'shown', 'after': 'unverified',
                            'error': str(exc)})
                        continue
                    if not scene['passed']:
                        route_regressions.extend(
                            {'route': original['original_file'],
                             'requirement_id': 'scene:' + fact['id'],
                             'before': 'shown', 'after': 'not_shown'}
                            for fact in scene['facts'] if not fact['covered'])
            write_json(output / 'collection/coverage.json',
                       {'requirement_coverage': final_coverage, 'demos': final_choices,
                        'route_regressions': route_regressions})
        if (digest(project) != fingerprint or digest(frozen) != fingerprint
                or any(file_hash(Path(p)) != h for p, h in source['metadata_hashes'].items())
                or any(file_hash(row['path']) != row['sha256'] or
                       (row.get('source_file') and
                        file_hash(Path(row['source_file'])) != row['source_sha256']) or
                       (row.get('source_result') and
                        file_hash(Path(row['source_result'])) != row['source_result_sha256'])
                       for row in retained + rsi_playtests)
                or digest(run_dir / 'initial/adapter') != source['adapter_hashes']):
            raise RuntimeError('RSI source artifacts changed during collection')
        coverage = final_coverage
        required_ids = manifest.get('required_coverage_ids', list(coverage))
        if mode in ('merged', 'preserve'):
            # Partial RSI progress is acceptable. The hard gate is that each
            # original route keeps the public evidence it already showed.
            missing = ['route:' + row['route'] + ':' + row['requirement_id']
                       for row in route_regressions]
        else:
            missing = [key for key in required_ids if coverage[key]['status'] != 'shown']
        result = {'state': 'coverage_incomplete' if missing else 'ready_for_judging', 'run': str(run_dir),
                  'selected': source['selected'], 'project': str(submission),
                  'collection': str(output / 'collection'),
                  'showcase_manifest': str(output / 'showcase/manifest.json'),
                  'demo_provenance': str(output / 'demo_provenance.json'),
                  'showcase_mode': mode,
                  'coverage_scope': ('initialization_video_scene_preservation'
                                     if mode == 'preserve' else
                                     'initialization_route_preservation_and_observed_rsi_progress'
                                     if mode == 'merged' else 'public_requirements_and_rsi_goals'),
                  'coverage_policy': ('preserve_original_visible_facts'
                                      if mode == 'preserve' else
                                      'preserve_each_initialization_route_allow_partial_rsi_goals'
                                      if mode == 'merged' else
                                      'preserve_observed_initialization_and_show_rsi_goals'
                                      if mode == 'incremental' else 'all_declared_goals'),
                  'retained_demo_count': sum(e['source'] == 'initialization_trace' for e in entries),
                  'reused_rsi_playtest_count': sum(e['source'] == 'rsi_playtest_trace' for e in entries),
                  'new_demo_count': sum(e['source'] in ('fresh_rsi_playtest', 'merged_rsi_trace',
                                                       'repaired_initialization_trace')
                                        for e in entries),
                  'merged_demo_count': sum(e['source'] == 'merged_rsi_trace' for e in entries),
                  'coverage_complete': not missing, 'not_fully_shown': missing,
                  'coverage_assessed': True,
                  'required_coverage_ids': required_ids,
                  'rsi_goals_not_fully_shown': [r['id'] for r in requirements
                      if r['source'] == 'rsi_goal' and coverage[r['id']]['status'] != 'shown'],
                  'public_requirements_not_shown': [r['id'] for r in requirements
                      if r['source'] == 'public_requirement' and coverage[r['id']]['status'] != 'shown'],
                  'official_benchmark_metric': False,
                  'source_state': info['state'], 'source_errors': info.get('errors', [])}
        write_json(output / 'result.json', result)
        write_json(output / 'status.json', result)
        if missing and mode != 'preserve':
            raise CoverageIncompleteError('Final scoring recordings have missing coverage: ' + ', '.join(missing))
        return submission
    except CoverageIncompleteError as exc:
        if not (output / 'result.json').is_file():
            incomplete = {'state': 'coverage_incomplete', 'showcase_mode': mode,
                          'coverage_complete': False, 'reason': str(exc), 'run': str(run_dir)}
            write_json(output / 'result.json', incomplete)
            write_json(output / 'status.json', incomplete)
        raise
    except Exception as exc:
        write_json(output / 'status.json', {'state': 'failed', 'error': f'{type(exc).__name__}: {exc}',
                                          'run': str(run_dir),
                                          'training_result': str(run_dir / 'result.json')})
        raise
