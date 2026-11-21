"""Evidence-based demo selection for post-RSI collection, never benchmark scores."""
from itertools import combinations
from pathlib import Path
import shutil
import subprocess

from gamecraft_behavior.core import digest, write_json
from gamecraft_training.package import final_showcase as showcase
from .packaging import validate_trace
from .trained_demos import file_hash, public_trace


class CoverageIncompleteError(RuntimeError):
    """Artifacts are preserved, but they are not ready for comparative scoring."""


def coverage_summary(requirements, choices):
    rank = {'not_planned': 0, 'unverified': 0, 'not_shown': 0, 'partial': 1, 'shown': 2}
    result = {r['id']: {'status': 'not_planned', 'profiles': []} for r in requirements}
    for choice in choices:
        for item in choice['coverage']:
            summary = result[item['id']]
            summary['profiles'].append(choice['row']['id'])
            if rank[item['status']] > rank[summary['status']]:
                summary['status'] = item['status']
    return result


def compact_choices(choices, limit=showcase.MAX_DEMOS):
    """Drop only clips whose observed coverage is preserved, including partial evidence.

    No score or aesthetic preference is used. With at most ten original clips
    and a few fresh profiles, an exact subset search avoids greedy coverage loss.
    """
    facts = []
    for choice in choices:
        facts.append({(x['id'], level) for x in choice['coverage']
                      for level in range({'shown': 2, 'partial': 1}.get(x['status'], 0))})
    required = set().union(*facts)
    for count in range(1, min(limit, len(choices)) + 1):
        for indices in combinations(range(len(choices)), count):
            if set().union(*(facts[i] for i in indices)) == required:
                return [choices[i] for i in indices]
    raise CoverageIncompleteError('Observed coverage cannot fit within the demo limit; '
                                  'keep the candidate evidence and consolidate routes.')


def run_incremental(project, output, *, demos, baseline_project, requirements, initial_profiles,
                    scenarios, previous, skill, adapter, godot, model, backend,
                    step_frames, policy_factory, rollout, assessor, planner):
    """Replay old inputs, record missing goals, and supplement only uncovered content."""
    project, output = Path(project), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = digest(project)
    replay_project = output / 'replay-project'
    shutil.copytree(project, replay_project,
                    ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
    cache = output.parent / '.final_showcase_replay_cache'
    choices, attempts = [], []
    required = [r['id'] for r in requirements]
    # No planner can delete old routes before observing what they actually cover.
    for index, demo in enumerate(demos, 1):
        if file_hash(demo['path']) != demo['sha256']:
            raise RuntimeError('Initialization trace changed before replay')
        record = {'trace': str(demo['path']), 'scenario': demo['scenario'],
                  'normal_launch': demo['scenario'] is None,
                  'project_sha256': fingerprint, 'player_policy': None}
        row = {'id': f'retained_{index:02d}', 'requirement_ids': required,
               'goal': 'Observe existing input route on the final game: ' + demo['file'],
               'scenario': demo['scenario'], 'source_file': demo.get('source_file', str(demo['path']))}
        try:
            official = showcase._cached_official_record(
                replay_project, record, output / 'official' / row['id'], godot,
                demo_name=row['id'], project_hash=fingerprint, cache_root=cache)
        except (RuntimeError, subprocess.SubprocessError) as exc:
            # A changed game can invalidate an old route. Preserve the error and
            # recover its old public coverage through baseline inspection below.
            attempts.append({'profile': row['id'], 'source_file': str(demo['path']),
                             'replay_error': str(exc)})
            write_json(output / 'retained_replay_errors.json', attempts)
            continue
        record.update(video=official['video'], replay_verified=True)
        coverage = assessor(row, official, requirements=requirements, backend=backend, model=model)
        choices.append({'row': row, 'record': record, 'official': official,
                        'coverage': coverage, 'source': demo.get('source', 'initialization_trace')})
    write_json(output / 'retained_coverage.json', [
        {'profile': c['row'], 'official': c['official'], 'coverage': c['coverage']}
        for c in choices])

    common = dict(replay_project=replay_project, fingerprint=fingerprint, cache=cache,
                  requirements=requirements, skill=skill, adapter=adapter, rollout=rollout,
                  godot=godot, model=model, backend=backend, step_frames=step_frames,
                  policy_factory=policy_factory, assessor=assessor)
    observed = coverage_summary(requirements, choices)
    # A game can already lack a public feature before RSI. Do not turn every
    # unmet specification into an impossible demonstration gate. For gaps, inspect
    # the original game with the same inputs to establish what must be preserved.
    public_ids = {r['id'] for r in requirements if r['source'] == 'public_requirement'}
    floor = {rid for rid in public_ids if observed[rid]['status'] == 'shown'}
    pending = public_ids - floor
    baseline_evidence = []
    initialization_demos = [demo for demo in demos
                            if demo.get('source', 'initialization_trace') == 'initialization_trace']
    if pending and initialization_demos:
        baseline_hash = digest(Path(baseline_project))
        baseline_copy = output / 'baseline-replay-project'
        shutil.copytree(baseline_project, baseline_copy,
                        ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
        for index, demo in enumerate(initialization_demos, 1):
            if not pending:
                break
            record = {'trace': str(demo['path']), 'scenario': demo['scenario'],
                      'normal_launch': demo['scenario'] is None}
            row = {'id': f'baseline_{index:02d}', 'requirement_ids': sorted(pending),
                   'goal': 'Establish existing initialization coverage: ' + demo['file']}
            official = showcase._cached_official_record(
                baseline_copy, record, output / 'official' / row['id'], godot,
                demo_name=row['id'], project_hash=baseline_hash, cache_root=cache)
            facts = assessor(row, official, requirements=requirements, backend=backend, model=model)
            baseline_evidence.append({'row': row, 'coverage': facts, 'official': official})
            shown = {f['id'] for f in facts if f['status'] == 'shown'}
            floor.update(shown)
            pending -= shown
        if digest(Path(baseline_project)) != baseline_hash:
            raise RuntimeError('Initialization game changed while checking coverage')
    write_json(output / 'baseline_coverage.json',
               {'required_public_ids': sorted(floor), 'not_shown_in_baseline': sorted(pending),
                'evidence': baseline_evidence})
    rows = [r for r in initial_profiles['profiles'] if any(
        observed[rid]['status'] != 'shown' for rid in r['retry_requirement_ids'])]
    if rows:
        seed_plan, tasks = showcase.validate_plan({'profiles': rows}, scenarios,
                                                  requirements, step_frames)
        fresh, history, _ = showcase._record_profiles(
            project, output, seed_plan, tasks, prefix='seed-', **common)
        choices.extend(fresh)
        attempts.extend(history)
    write_json(output / 'candidate_coverage.json', [
        {'profile': c['row'], 'official': c['official'], 'coverage': c['coverage']}
        for c in choices])
    selected = compact_choices(choices)
    summary = coverage_summary(requirements, selected)
    missing = {rid for rid, value in summary.items() if value['status'] != 'shown'}
    slots = showcase.MAX_DEMOS - len(selected)
    if missing and slots:
        uncovered = [r for r in requirements if r['id'] in missing]
        context = previous + [{'profile': c['row']['id'], 'visible_coverage': c['coverage']}
                              for c in selected]
        proposal = planner(adapter, project, scenarios, uncovered, context,
                           backend=backend, model=model, step_frames=step_frames,
                           max_demos=slots, uncovered_requirements=missing)
        plan, tasks = showcase.validate_plan(proposal, scenarios, uncovered,
                                             step_frames, max_demos=slots)
        # Keep IDs distinct even if the planner reuses a seed-profile name.
        plan['profiles'] = [{**r, 'id': f'gap_{i:02d}'}
                            for i, r in enumerate(plan['profiles'], 1)]
        for row, task in zip(plan['profiles'], tasks):
            task['profile'] = 'showcase/' + row['id']
        extra, history, _ = showcase._record_profiles(
            project, output, plan, tasks, prefix='gap-', **common)
        choices.extend(extra)
        attempts.extend(history)
        selected = compact_choices(choices)
    if digest(project) != fingerprint:
        raise RuntimeError('Final game changed during showcase recording')
    demo_dir = output / 'demo_outputs'
    demo_dir.mkdir()
    entries = []
    for index, choice in enumerate(selected, 1):
        path = demo_dir / f'{index:02d}_{choice["row"]["id"]}.json'
        write_json(path, public_trace(choice['record']))
        validate_trace(path)
        if file_hash(path) != file_hash(Path(choice['official']['trace'])):
            raise ValueError('Selected trace differs from observed official replay')
        entries.append({'name': path.name, 'profile': choice['row']['id'],
                        'source': choice.get('source', 'fresh_rsi_playtest'),
                        'source_trace': choice['record']['trace'],
                        'official_video': choice['official']['video'],
                        'coverage': choice['coverage']})
    summary = coverage_summary(requirements, selected)
    manifest = {'status': 'official_replays_collected', 'project_sha256': fingerprint,
                'demos': entries, 'attempts': attempts, 'requirement_coverage': summary,
                'coverage': [{'profile': c['row']['id'], **x}
                             for c in selected for x in c['coverage']],
                'initialization_routes_checked': len(initialization_demos),
                'rsi_playtest_routes_checked': len(demos) - len(initialization_demos),
                'required_coverage_ids': sorted(floor | {
                    rid for rid, value in summary.items() if value['status'] == 'shown'} | {
                    r['id'] for r in requirements if r['source'] == 'rsi_goal'}),
                'selection': 'Smallest subset preserving all observed public and RSI coverage',
                'bootstrap_scene_floor_passed': None}
    write_json(output / 'manifest.json', manifest)
    return manifest


def require_ready_for_scoring(project, evidence=None):
    """Reject incomplete post-RSI collections even if a caller invokes scoring directly."""
    roots = {Path(project).resolve().parent}
    if evidence is not None:
        roots.add(Path(evidence).resolve().parent)
    for root in roots:
        path = root / 'result.json'
        config_path = root / 'configuration.json'
        import json
        if not path.is_file():
            if config_path.is_file() and str(json.loads(config_path.read_text()).get(
                    'protocol', '')).startswith(('post-rsi-showcase-', 'post-rsi-score-only-')):
                raise CoverageIncompleteError(f'Post-RSI collection has no completed result: {root}')
            continue
        result = json.loads(path.read_text())
        if result.get('showcase_mode') == 'score_only':
            if (not config_path.is_file() or json.loads(config_path.read_text()).get(
                    'protocol') != 'post-rsi-score-only-v1' or
                    result.get('state') != 'ready_for_judging' or
                    result.get('replay_complete') is not True or
                    result.get('coverage_assessed') is not False):
                raise CoverageIncompleteError(f'Post-RSI score-only replay is incomplete: {path}')
            continue
        if result.get('showcase_mode') == 'preserve':
            collection = root / 'collection/collection.json'
            if (result.get('state') not in ('ready_for_judging', 'coverage_incomplete')
                    or result.get('coverage_assessed') not in (False, True)
                    or not collection.is_file()):
                raise CoverageIncompleteError(f'Post-RSI preserve replay is incomplete: {path}')
            recorded = json.loads(collection.read_text())
            if recorded.get('build_ok') is not True or recorded.get('errors'):
                raise CoverageIncompleteError(f'Post-RSI preserve replay failed: {collection}')
            if (result.get('coverage_assessed') is True and
                    not (root / 'collection/coverage.json').is_file()):
                raise CoverageIncompleteError(f'Post-RSI preserve coverage is missing: {root}')
            continue
        if 'showcase_mode' not in result:
            continue
        if result.get('coverage_complete') is not True or result.get('state') != 'ready_for_judging':
            raise CoverageIncompleteError(f'Post-RSI coverage is incomplete: {path}; '
                                          'repair demonstration coverage before scoring.')
