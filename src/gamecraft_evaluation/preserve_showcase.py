"""Preserve initialization video evidence on the final RSI game."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from play2evolve.runtime import configure_benchmark
import json
import os
import shutil
import subprocess
import sys

from gamecraft_behavior.core import digest, write_json
from gamecraft_training.package import final_showcase as showcase

from . import rsi_baseline_comparison
from .merged_showcase import compare_scenes, original_policy
from .packaging import validate_trace
from .showcase_coverage import coverage_summary
from .trained_demos import file_hash, public_trace

MAX_REPAIR_ATTEMPTS = 2


def covered_scene_count(scene, original_facts):
    """Count only facts established in the initialization video."""
    protected = {fact['id'] for fact in original_facts}
    return len({fact['id'] for fact in scene['facts']
                if fact['id'] in protected and fact['covered']})


def _isolated_official_record(project, record, directory, godot, *, demo_name,
                              project_hash, cache_root):
    """Replay in a separate process so concurrent Godot caches cannot collide."""
    directory = Path(directory)
    job_dir = directory.parent / f'.{directory.name}-job'
    job_dir.mkdir(parents=True, exist_ok=False)
    request = job_dir / 'request.json'
    response = job_dir / 'response.json'
    write_json(request, {'project': str(project), 'record': record,
                         'directory': str(directory), 'godot': str(godot),
                         'demo_name': demo_name, 'project_hash': project_hash,
                         'cache_root': str(cache_root)})
    env = os.environ.copy()
    for name in ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
        location = job_dir / name.lower()
        location.mkdir()
        env[name] = str(location.resolve())
    log = job_dir / 'worker.log'
    with log.open('w') as stream:
        result = subprocess.run(
            [sys.executable, '-m', 'gamecraft_evaluation.preserve_showcase',
             '--record-worker', str(request), str(response)],
            stdout=stream, stderr=subprocess.STDOUT, env=env, timeout=300)
    if result.returncode or not response.is_file():
        raise RuntimeError(f'Isolated official replay failed; see {log}')
    return json.loads(response.read_text())


def _record_worker(request_path, response_path):
    args = json.loads(Path(request_path).read_text())
    result = showcase._cached_official_record(
        args['project'], args['record'], args['directory'], args['godot'],
        demo_name=args['demo_name'], project_hash=args['project_hash'],
        cache_root=args['cache_root'])
    write_json(Path(response_path), result)


def run_preserve(project, output, *, demos, baseline_project, initial_root,
                 requirements, scenarios, skill, adapter, godot, model, backend,
                 step_frames, policy_factory, rollout, parallelism,
                 rsi_playtests=(), assessor=None,
                 comparator=rsi_baseline_comparison.compare_baseline):
    """Keep one slot per original trace; compare reuse and repair candidates.

    Public and RSI rubric goals are deliberately not used as a retry threshold.
    The original video is the coverage floor for each route.
    """
    project, output = Path(project), Path(output)
    if not 1 <= len(demos) <= showcase.MAX_DEMOS:
        raise ValueError('Preserve showcase needs 1–10 initialization routes')
    if type(parallelism) is not int or not 1 <= parallelism <= showcase.MAX_DEMOS:
        raise ValueError('Preserve parallelism must be between 1 and 10')
    benchmark_root = configure_benchmark()
    if benchmark_root.is_dir() and str(benchmark_root) not in sys.path:
        sys.path.insert(0, str(benchmark_root))
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = digest(project)
    baseline_source = Path(baseline_project)
    baseline_hash = digest(baseline_source)
    baseline_copy = output / 'baseline-project'
    replay_copy = output / 'replay-project'
    shutil.copytree(baseline_source, baseline_copy,
                    ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
    shutil.copytree(project, replay_copy,
                    ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
    if digest(baseline_copy) != baseline_hash or digest(replay_copy) != fingerprint:
        raise RuntimeError('Game changed while freezing the preserve showcase')
    cache = Path(os.environ.get('GAMECRAFT_SHOWCASE_REPLAY_CACHE',
                                output.parent / '.final_showcase_replay_cache'))
    # Test doubles remain in-process; production replays need isolated Godot
    # user/cache directories because _official_record mutates os.environ.
    record_official = (showcase._cached_official_record
                       if showcase._official_record.__module__ != showcase.__name__
                       else _isolated_official_record)

    def inspect(index, demo):
        if file_hash(demo['path']) != demo['sha256']:
            raise RuntimeError('Initialization trace changed before replay')
        route_id = f'init_{index:02d}'
        record = {'trace': str(demo['path']), 'scenario': demo['scenario'],
                  'normal_launch': demo['scenario'] is None,
                  'project_sha256': fingerprint, 'player_policy': None}
        original = record_official(
            baseline_copy, record, output / 'baseline' / route_id, godot,
            demo_name=route_id, project_hash=baseline_hash, cache_root=cache)
        final = record_official(
            replay_copy, record, output / 'official' / route_id, godot,
            demo_name=route_id, project_hash=fingerprint, cache_root=cache)
        goal = ('Preserve the same distinct game modes, interactions, transitions, '
                'and major outcomes visible in ' + demo['file'] + '. '
                'Do not require identical transient score counters, animation frames, '
                'or the same instant within an interaction. Ignore engine splash '
                'screens, loading frames, and capture artifacts.')
        scene = compare_scenes(
            comparator, {'id': route_id, 'goal': goal}, original['video'], final,
            output / 'scenes' / route_id, backend=backend, model=model)
        return {'index': index, 'demo': demo, 'record': record, 'baseline': original,
                'official': final, 'scene': scene, 'goal': goal,
                'source': 'initialization_trace'}

    # The first replay imports the two shared projects. Once imported, routes
    # use independent recording directories and can safely replay in parallel.
    choices = [inspect(1, demos[0])]
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        choices.extend(pool.map(lambda pair: inspect(*pair),
                                enumerate(demos[1:], 2)))
    if assessor is not None:
        requirement_ids = [r['id'] for r in requirements]
        for item in choices:
            row = {'id': f'preserve_{item["index"]:02d}_original',
                   'requirement_ids': requirement_ids,
                   'goal': 'Record the public and RSI outcomes visible in this route.'}
            item['coverage'] = assessor(row, item['official'],
                                        requirements=requirements,
                                        backend=backend, model=model)
        initial_coverage = coverage_summary(requirements, [
            {'row': {'id': f'preserve_{item["index"]:02d}_original'},
             'coverage': item['coverage']} for item in choices])
    else:
        initial_coverage = {}
    write_json(output / 'initial_coverage.json', {
        'requirement_coverage': initial_coverage,
        'routes': [{'file': item['demo']['file'], 'coverage': item.get('coverage', [])}
                   for item in choices]})
    write_json(output / 'baseline_scene_coverage.json', [
        {'file': item['demo']['file'], 'scenario': item['demo']['scenario'],
         'passed': item['scene']['passed'], 'facts': item['scene']['facts']}
        for item in choices])
    cases = {case['id']: case for case in scenarios}
    normal = next((case for case in scenarios if case.get('launch') == 'default'), None)
    required_id = requirements[0]['id'] if requirements else None
    if required_id is None:
        raise ValueError('Preserve showcase needs a declared requirement')
    goal_ids = [r['id'] for r in requirements
                if r['source'] == 'rsi_goal' and
                initial_coverage.get(r['id'], {}).get('status') != 'shown']

    def goal_score(route_id, official, observations=None):
        if not goal_ids or assessor is None:
            return 0
        if observations is None:
            row = {'id': route_id, 'requirement_ids': goal_ids,
                   'goal': 'Check visible RSI gameplay and presentation goals.'}
            observations = assessor(row, official, requirements=requirements,
                                    backend=backend, model=model)
        rank = {'not_planned': 0, 'not_shown': 0, 'unverified': 0,
                'partial': 1, 'shown': 2}
        return sum(rank[item['status']] for item in observations
                   if item['id'] in goal_ids)

    def repair(item):
        if item['scene'].get('status') in ('unverified', 'no_baseline_facts'):
            return item, {'file': item['demo']['file'], 'accepted': False,
                          'scene_preserved': False,
                          'selected_source': 'initialization_trace',
                          'acceptance_reason': item['scene']['status'],
                          'attempts': []}
        compatible = [demo for demo in rsi_playtests
                      if demo['scenario'] == item['demo']['scenario']]
        if item['scene']['passed'] and (not compatible or not goal_ids):
            return item, None
        route_id = f'preserve_{item["index"]:02d}'
        scenario = item['demo']['scenario']
        source_case = normal if scenario is None else cases.get(scenario)
        if source_case is None:
            raise ValueError('Initialization route has no declared launch')
        row = {'id': route_id, 'scenario': scenario, 'requirement_ids': [required_id],
               'goal': ('On the current game, reproduce every original_scene_fact. '
                        'Use original_policy and original_trace as references, but '
                        'adapt to current observations and timing.'),
               'expected': 'Show every scene and outcome visible in the initialization video.',
               'max_decisions': showcase.MAX_FRAMES // step_frames,
               'original_scene_facts': item['scene']['facts'],
               'original_trace': json.loads(item['demo']['path'].read_text()),
               'original_policy': original_policy(initial_root, scenario)}
        best = item
        original_facts = item['scene']['facts']
        best_count = covered_scene_count(item['scene'], original_facts)
        best_goal_score = None
        best_label = route_id + '_original'

        def improves(scene, official, label):
            nonlocal best_goal_score
            count = covered_scene_count(scene, original_facts)
            if count != best_count:
                return count > best_count, count, None
            if best_goal_score is None:
                best_goal_score = goal_score(
                    best_label, best['official'],
                    item['coverage'] if best is item and 'coverage' in item else None)
            candidate_goal_score = goal_score(label, official)
            return candidate_goal_score > best_goal_score, count, candidate_goal_score

        missing = [fact for fact in item['scene']['facts'] if not fact['covered']]
        attempts = []
        for playtest in compatible:
            if file_hash(playtest['path']) != playtest['sha256']:
                raise RuntimeError('RSI playtest trace changed before replay')
            reuse_id = f'{route_id}_rsi_{Path(playtest["file"]).stem}'
            record = {'trace': str(playtest['path']), 'scenario': scenario,
                      'normal_launch': scenario is None,
                      'project_sha256': fingerprint, 'player_policy': None}
            try:
                official = record_official(
                    replay_copy, record, output / 'candidate-official' / reuse_id,
                    godot, demo_name=reuse_id, project_hash=fingerprint, cache_root=cache)
                scene = compare_scenes(
                    comparator, {'id': reuse_id, 'goal': item['goal']},
                    item['baseline']['video'], official,
                    output / 'candidate-check' / reuse_id,
                    backend=backend, model=model, facts=item['scene']['facts'])
                improved, count, candidate_goal_score = improves(scene, official, reuse_id)
            except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                attempts.append({'source': 'rsi_playtest_trace',
                                 'file': playtest['file'], 'error': str(exc)})
                continue
            attempts.append({'source': 'rsi_playtest_trace', 'file': playtest['file'],
                             'video': official['video'], 'scene_preserved': scene['passed'],
                             'covered_scene_count': count,
                             'rsi_goal_score': candidate_goal_score})
            if improved:
                best = {**item, 'record': record, 'official': official,
                        'scene': scene, 'source': 'rsi_playtest_trace'}
                best_count = count
                best_goal_score = candidate_goal_score
                best_label = reuse_id
                missing = [fact for fact in scene['facts'] if not fact['covered']]
        if best['scene']['passed']:
            if best is item:
                return item, None
            return best, {'file': item['demo']['file'], 'accepted': True,
                          'scene_preserved': True, 'selected_source': best['source'],
                          'covered_scene_count': best_count, 'attempts': attempts}
        for number in range(1, MAX_REPAIR_ATTEMPTS + 1):
            attempt_id = f'attempt-{number:02d}'
            current = {**row, 'missing_scene_facts': missing,
                       'goal': row['goal'] + ' Prioritize these still-missing visible facts: '
                               + '; '.join(fact['description'] for fact in missing)}
            plan, tasks = showcase.validate_plan(
                {'profiles': [current]}, scenarios, requirements, step_frames)
            context = {'phase': 'final_showcase', 'showcase_plan': plan,
                       'behavior_skill_enabled': bool(skill)}
            try:
                record = showcase.play_version(
                    project, tasks, output / 'candidate' / route_id / attempt_id,
                    profiles=showcase.profiles_for(tasks), prior=None, reuse=False,
                    skill=skill, context=context, adapter=adapter, rollout=rollout,
                    godot=godot, model=model, backend=backend, step_frames=step_frames,
                    max_decisions=showcase.MAX_FRAMES // step_frames,
                    factory=policy_factory)[0]
                official = record_official(
                    replay_copy, record,
                    output / 'candidate-official' / route_id / attempt_id,
                    godot, demo_name=route_id, project_hash=fingerprint, cache_root=cache)
                scene = compare_scenes(
                    comparator, {'id': route_id + '_' + attempt_id, 'goal': item['goal']},
                    item['baseline']['video'], official,
                    output / 'candidate-check' / route_id / attempt_id,
                    backend=backend, model=model, facts=item['scene']['facts'])
            except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                attempts.append({'attempt': number, 'error': str(exc)})
                continue
            improved, count, candidate_goal_score = improves(
                scene, official, route_id + '_' + attempt_id)
            attempts.append({'attempt': number, 'video': official['video'],
                             'scene_preserved': scene['passed'],
                             'covered_scene_count': count,
                             'rsi_goal_score': candidate_goal_score,
                             'missing_scene_facts': [fact for fact in scene['facts']
                                                     if not fact['covered']]})
            if improved:
                best = {**item, 'record': record, 'official': official,
                        'scene': scene, 'source': 'repaired_initialization_trace'}
                best_count = count
                best_goal_score = candidate_goal_score
                best_label = route_id + '_' + attempt_id
                missing = [fact for fact in scene['facts'] if not fact['covered']]
            if best['scene']['passed']:
                break
        accepted = best is not item
        return best, {'file': item['demo']['file'], 'accepted': accepted,
                      'scene_preserved': best['scene']['passed'],
                      'selected_source': best['source'],
                      'covered_scene_count': best_count,
                      'acceptance_reason': ('scene_preserved' if best['scene']['passed'] else
                                            'most_visible_facts' if accepted else
                                            'original_more_visible'),
                      'attempts': attempts}

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        repaired = list(pool.map(repair, choices))
    choices = [item for item, _ in repaired]
    reports = [report for _, report in repaired if report is not None]
    write_json(output / 'repair_results.json', reports)
    if digest(project) != fingerprint:
        raise RuntimeError('Final game changed during preserve recording')
    demo_dir = output / 'demo_outputs'
    demo_dir.mkdir()
    entries = []
    for item in choices:
        index, demo = item['index'], item['demo']
        path = demo_dir / f'{index:02d}_{Path(demo["file"]).stem}.json'
        write_json(path, public_trace(item['record']))
        validate_trace(path)
        entries.append({'name': path.name, 'original_file': demo['file'],
                        'source': item['source'], 'scenario': demo['scenario'],
                        'source_trace': item['record']['trace'],
                        'baseline_video': item['baseline']['video'],
                        'baseline_scene_status': item['scene'].get('status', 'verified'),
                        'baseline_scene_facts': item['scene']['facts'],
                        'official_video': item['official']['video'],
                        'trace_sha256': file_hash(path)})
    manifest = {'status': 'official_replays_collected',
                'project_sha256': fingerprint, 'demos': entries,
                'repair_results': reports, 'requirement_coverage': {},
                'required_coverage_ids': [], 'initialization_routes_checked': len(demos),
                'initial_coverage': str(output / 'initial_coverage.json'),
                'initially_missing_rsi_goals': goal_ids,
                'parallelism': parallelism, 'coverage_assessed': False,
                'selection': 'One output per initialization route; prefer more original visible facts, then RSI goal evidence'}
    write_json(output / 'manifest.json', manifest)
    return manifest


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--record-worker':
        raise SystemExit('Usage: python -m gamecraft_evaluation.preserve_showcase '
                         '--record-worker REQUEST RESPONSE')
    _record_worker(sys.argv[2], sys.argv[3])
