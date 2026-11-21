"""Merge RSI goals into initialization routes without dropping a route."""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess

from gamecraft_behavior.core import digest, write_json
from gamecraft_training.package import final_showcase as showcase
from . import rsi_baseline_comparison
from .packaging import validate_trace
from .trained_demos import file_hash, public_trace
from .showcase_coverage import coverage_summary


def original_policy(initial_root, scenario):
    """Return an archived initialization play program for this launch, if unique."""
    root = Path(initial_root) / 'final_recordings'
    manifest = root / 'manifest.json'
    if not manifest.is_file():
        return None
    groups = json.loads(manifest.read_text()).get('groups', {})
    matches = []
    for group in groups.values() if isinstance(groups, dict) else groups:
        for episode in group.get('episodes', []):
            if not (episode.get('scenario') == scenario or
                    scenario is None and episode.get('normal_launch') is True):
                continue
            item = episode.get('files', {}).get('player_policy', {})
            relative = Path(item.get('path', ''))
            if (not relative.parts or relative.is_absolute()
                    or '..' in relative.parts):
                continue
            path = root / relative
            if path.is_file() and file_hash(path) == item.get('sha256'):
                matches.append(path)
    unique = {file_hash(path): path for path in matches}
    return next(iter(unique.values())).read_text() if len(unique) == 1 else None


def compare_scenes(comparator, route, baseline_video, official, output, *,
                   backend, model, facts=None):
    return comparator({'id': route['id'], 'goal': route['goal'],
                       'bootstrap_video': str(baseline_video)}, official,
                      backend=backend, model=model, output=output, facts=facts)


def validate_assignments(proposal, routes, goal_ids):
    """Require an explicit home for every RSI goal and no invented route."""
    rows = proposal.get('assignments') if isinstance(proposal, dict) else None
    available = {route['file'] for route in routes}
    if not isinstance(rows, list):
        raise ValueError('Merge planner omitted assignments')
    merged = {}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid merge assignment')
        name, ids, goal = row.get('source_file'), row.get('goal_ids'), row.get('goal')
        if (name not in available or name in merged or not isinstance(ids, list) or not ids
                or not isinstance(goal, str) or not goal.strip()
                or any(rid not in goal_ids or rid in seen for rid in ids)):
            raise ValueError('Merge planner used an unknown route or duplicate RSI goal')
        merged[name] = {'goal_ids': ids, 'goal': goal.strip()}
        seen.update(ids)
    if seen != set(goal_ids):
        raise ValueError('Merge planner left RSI goals without a route')
    return merged


def preserves(old, new, protected_ids):
    """A replacement may not weaken any public fact visible in this route."""
    rank = {'not_planned': 0, 'not_shown': 0, 'unverified': 0,
            'partial': 1, 'shown': 2}
    before = {item['id']: rank[item['status']] for item in old}
    after = {item['id']: rank[item['status']] for item in new}
    return all(after.get(rid, 0) >= before[rid] for rid in protected_ids)


def assess_with_retry(assessor, row, official, **kwargs):
    """Retry omissions, then ask smaller questions without inventing coverage."""
    for attempt in range(2):
        try:
            return assessor(row, official, **kwargs)
        except ValueError as exc:
            if not attempt:
                continue
            ids = row.get('requirement_ids', [])
            if (str(exc) != 'Final showcase coverage assessment omitted planned requirements'
                    or len(ids) < 2):
                raise
            middle = len(ids) // 2
            return (assess_with_retry(assessor, {**row, 'requirement_ids': ids[:middle]},
                                      official, **kwargs)
                    + assess_with_retry(assessor, {**row, 'requirement_ids': ids[middle:]},
                                        official, **kwargs))


def run_merged(project, output, *, demos, baseline_project, initial_root,
               requirements, scenarios, skill,
               adapter, godot, model, backend, step_frames, policy_factory,
               rollout, assessor, planner, comparator=rsi_baseline_comparison.compare_baseline):
    """Replay every initialization input, then rewrite only compatible routes."""
    project, output = Path(project), Path(output)
    if not 1 <= len(demos) <= showcase.MAX_DEMOS:
        raise ValueError('Merged showcase needs 1–10 initialization routes')
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = digest(project)
    baseline_source = Path(baseline_project)
    baseline_hash = digest(baseline_source)
    if not baseline_hash:
        raise ValueError('Initialization game is missing')
    baseline_project = output / 'baseline-project'
    shutil.copytree(baseline_source, baseline_project,
                    ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
    if digest(baseline_project) != baseline_hash:
        raise RuntimeError('Initialization game changed while freezing it')
    replay_project = output / 'replay-project'
    shutil.copytree(project, replay_project,
                    ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
    cache = output.parent / '.final_showcase_replay_cache'
    public_ids = {r['id'] for r in requirements if r['source'] == 'public_requirement'}
    goal_ids = {r['id'] for r in requirements if r['source'] == 'rsi_goal'}
    choices, reports = [], []
    for index, demo in enumerate(demos, 1):
        if file_hash(demo['path']) != demo['sha256']:
            raise RuntimeError('Initialization trace changed before merged replay')
        record = {'trace': str(demo['path']), 'scenario': demo['scenario'],
                  'normal_launch': demo['scenario'] is None,
                  'project_sha256': fingerprint, 'player_policy': None}
        row = {'id': f'init_{index:02d}', 'requirement_ids': [r['id'] for r in requirements],
               'goal': 'Preserve initialization route ' + demo['file'],
               'scenario': demo['scenario']}
        baseline = showcase._cached_official_record(
            baseline_project, record, output / 'baseline' / row['id'], godot,
            demo_name=row['id'], project_hash=baseline_hash, cache_root=cache)
        baseline_coverage = assess_with_retry(
            assessor, row, baseline, requirements=requirements,
            backend=backend, model=model)
        official = showcase._cached_official_record(
            replay_project, record, output / 'official' / row['id'], godot,
            demo_name=row['id'], project_hash=fingerprint, cache_root=cache)
        record.update(video=official['video'], replay_verified=True)
        coverage = assess_with_retry(assessor, row, official,
                                     requirements=requirements,
                                     backend=backend, model=model)
        protected = {item['id'] for item in baseline_coverage
                     if item['id'] in public_ids and item['status'] in ('partial', 'shown')}
        scene = compare_scenes(
            comparator, row, baseline['video'], official,
            output / 'scenes' / row['id'], backend=backend, model=model)
        choices.append({'row': row, 'record': record, 'official': official,
                        'coverage': coverage, 'original_coverage': baseline_coverage,
                        'baseline_video': baseline['video'], 'scene': scene,
                        'protected': protected,
                        'source': 'initialization_trace', 'source_file': demo['file']})
    write_json(output / 'baseline_coverage.json', [
        {'file': c['source_file'], 'scenario': c['row']['scenario'],
         'protected_public_ids': sorted(c['protected']),
         'coverage': c['original_coverage'], 'final_coverage': c['coverage'],
         'scene_facts': c['scene']['facts'], 'scene_passed': c['scene']['passed']}
        for c in choices])
    observed = coverage_summary(requirements, choices)
    missing = {rid for rid in goal_ids if observed[rid]['status'] != 'shown'}
    if missing:
        assignments = validate_assignments(planner(
            project, demos, requirements, choices, missing,
            backend=backend, model=model), demos, missing)
    else:
        assignments = {}
    write_json(output / 'merge_plan.json', {'assignments': assignments,
               'unassigned_rsi_goals': sorted(missing - {
                   rid for row in assignments.values() for rid in row['goal_ids']})})
    cases = {case['id']: case for case in scenarios}
    normal = next((case for case in scenarios if case.get('launch') == 'default'), None)
    common = dict(replay_project=replay_project, fingerprint=fingerprint, cache=cache,
                  requirements=requirements, skill=skill, adapter=adapter,
                  rollout=rollout, godot=godot, model=model, backend=backend,
                  step_frames=step_frames, policy_factory=policy_factory,
                  assessor=lambda row, official, **kwargs:
                      assess_with_retry(assessor, row, official, **kwargs))
    for index, current in enumerate(choices):
        assignment = assignments.get(current['source_file'])
        lost_public = not preserves(current['original_coverage'],
                                    current['coverage'], current['protected'])
        lost_scenes = not current['scene']['passed']
        if assignment is None and not (lost_public or lost_scenes):
            continue
        source_case = normal if current['row']['scenario'] is None else cases[current['row']['scenario']]
        if source_case is None:
            raise ValueError('Initialization route has no declared normal launch')
        protected = current['protected']
        assigned_ids = set(assignment['goal_ids']) if assignment else set()
        ids = sorted(protected | assigned_ids) or sorted(public_ids)[:1]
        previous_policy = original_policy(initial_root, current['row']['scenario'])
        row = {'id': f'merge_{index + 1:02d}', 'scenario': current['row']['scenario'],
               'goal': ('Restore all visible baseline scene facts and public outcomes. '
                        'Use the original program and trace as references, adapting '
                        'actions to the current game state. ' +
                        (assignment['goal'] if assignment else '')),
               'expected': 'Show the original route outcomes and the assigned RSI goals.',
               'requirement_ids': ids, 'retry_requirement_ids': ids,
               'original_trace': json.loads(demos[index]['path'].read_text()),
               'original_policy': previous_policy,
               'original_scene_facts': current['scene']['facts'],
               'original_observations': [item for item in current['original_coverage']
                                         if item['id'] in protected],
               'max_decisions': showcase.MAX_FRAMES // step_frames}
        plan, tasks = showcase.validate_plan({'profiles': [row]}, scenarios,
                                             requirements, step_frames)
        try:
            fresh, attempts, _ = showcase._record_profiles(
                project, output, plan, tasks, prefix=f'merge-{index + 1:02d}-', **common)
        except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            reports.append({'file': current['source_file'], 'scenario': row['scenario'],
                            'rsi_goal_ids': sorted(assigned_ids), 'accepted': False,
                            'protected_public_ids': sorted(protected),
                            'candidate_error': str(exc)})
            write_json(output / 'merge_results.json', reports)
            continue
        candidate = fresh[0]
        candidate_by_id = {item['id']: item for item in candidate['coverage']}
        full_coverage = [candidate_by_id.get(item['id'], item)
                         for item in current['coverage']]
        try:
            candidate_scene = compare_scenes(
                comparator, row, current['baseline_video'], candidate['official'],
                output / 'candidate-scenes' / row['id'], backend=backend, model=model,
                facts=current['scene']['facts'])
        except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            reports.append({'file': current['source_file'], 'scenario': row['scenario'],
                            'rsi_goal_ids': sorted(assigned_ids), 'accepted': False,
                            'protected_public_ids': sorted(protected),
                            'candidate_error': str(exc)})
            write_json(output / 'merge_results.json', reports)
            continue
        upgrade_evidence = (not assigned_ids or any(
            candidate_by_id.get(rid, {}).get('status') in ('partial', 'shown')
            for rid in assigned_ids))
        accepted = (preserves(current['original_coverage'], full_coverage, protected)
                    and candidate_scene['passed'] and upgrade_evidence)
        reports.append({'file': current['source_file'], 'scenario': row['scenario'],
                        'rsi_goal_ids': sorted(assigned_ids), 'accepted': accepted,
                        'lost_public': lost_public, 'lost_scenes': lost_scenes,
                        'scene_passed': candidate_scene['passed'],
                        'protected_public_ids': sorted(protected),
                        'upgrade_evidence': {rid: candidate_by_id.get(rid, {}).get('status')
                                             for rid in assigned_ids},
                        'attempts': attempts})
        if accepted:
            choices[index] = {**candidate, 'coverage': full_coverage,
                              'original_coverage': current['original_coverage'],
                              'protected': protected, 'scene': candidate_scene,
                              'baseline_video': current['baseline_video'],
                              'source': 'merged_rsi_trace',
                              'source_file': current['source_file']}
    write_json(output / 'merge_results.json', reports)
    if digest(project) != fingerprint:
        raise RuntimeError('Final game changed during merged recording')
    demo_dir = output / 'demo_outputs'
    demo_dir.mkdir()
    entries = []
    for index, choice in enumerate(choices, 1):
        path = demo_dir / f'{index:02d}_{Path(choice["source_file"]).stem}.json'
        write_json(path, public_trace(choice['record']))
        validate_trace(path)
        entries.append({'name': path.name, 'source': choice['source'],
                        'original_file': choice['source_file'],
                        'scenario': choice['row']['scenario'],
                        'protected_public_ids': sorted(choice['protected']),
                        'original_coverage': choice['original_coverage'],
                        'baseline_video': choice['baseline_video'],
                        'baseline_scene_status': choice['scene'].get('status', 'verified'),
                        'baseline_scene_facts': choice['scene']['facts'],
                        'scene_passed': choice['scene']['passed'],
                        'source_trace': choice['record']['trace'],
                        'official_video': choice['official']['video'],
                        'coverage': choice['coverage'], 'trace_sha256': file_hash(path)})
    summary = coverage_summary(requirements, choices)
    manifest = {'status': 'official_replays_collected', 'project_sha256': fingerprint,
                'demos': entries, 'merge_results': reports,
                'requirement_coverage': summary,
                'required_coverage_ids': [],
                'required_route_evidence': {
                    c['source_file']: sorted(c['protected']) for c in choices},
                'initialization_routes_checked': len(demos),
                'selection': 'One output per initialization route; verified RSI merge only'}
    write_json(output / 'manifest.json', manifest)
    return manifest
