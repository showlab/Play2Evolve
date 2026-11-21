"""Plan and record final-version demos separately from initialization A/B playtests."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from itertools import combinations
from pathlib import Path
from play2evolve.runtime import benchmark_root, configure_benchmark

from gamecraft_behavior.core import digest, write_json
from gamecraft_composition.core import sources
from gamecraft_evaluation.packaging import validate_trace
from gamecraft_evaluation.trained_demos import public_trace

from . import models
from .version_player import play_version, profiles_for


MAX_DEMOS = 10
MAX_FRAMES = 600


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _replay_key(project_hash, record, godot, demo_name):
    """Include every input that can change the official replay or sampled frames."""
    benchmark = benchmark_root() / 'gamecraft_bench/verifier'
    binary = Path(shutil.which(str(godot)) or godot).resolve()
    stat = binary.stat() if binary.is_file() else None
    trace_bytes = (json.dumps(public_trace(record), indent=2,
                              ensure_ascii=False) + '\n').encode()
    inputs = {'project': project_hash,
              'asset_import_protocol': 1,
              'trace_bytes_sha256': hashlib.sha256(trace_bytes).hexdigest(),
              'godot': str(binary), 'godot_size': stat.st_size if stat else None,
              'godot_mtime_ns': stat.st_mtime_ns if stat else None,
              'replay_code': _sha(benchmark / 'replay.py'),
              'sampling_code': _sha(benchmark / 'score.py'),
              'sampling_seed': demo_name, 'sampling_interval': 0.5,
              'sampling_window': 20}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def _cached_official_record(project, record, directory, godot, *, demo_name,
                            project_hash, cache_root):
    """Reuse only a complete, hash-checked official replay of identical inputs."""
    directory, cache_root = Path(directory), Path(cache_root)
    key = _replay_key(project_hash, record, godot, demo_name)
    cached = cache_root / key
    manifest = cached / 'manifest.json'
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text())
            files = data['files']
            if (data['key'] == key and files and
                    {'demo.json', 'official.mp4'}.issubset(files) and
                    isinstance(data['frames'], list) and data['frames'] and
                    all(not Path(name).is_absolute() and '..' not in Path(name).parts
                        for name in files) and
                    set(data['frames']).issubset(files) and
                    all((cached / name).is_file() and _sha(cached / name) == checksum
                        for name, checksum in files.items())):
                directory.mkdir(parents=True, exist_ok=False)
                for name in files:
                    dest = directory / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cached / name, dest)
                return {'trace': str(directory / 'demo.json'),
                        'video': str(directory / 'official.mp4'),
                        'frames': [str(directory / name) for name in data['frames']],
                        'duration_seconds': data['duration_seconds'],
                        'trace_seconds': data['trace_seconds'], 'cache_hit': True}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    result = _official_record(project, record, directory, godot, demo_name=demo_name)
    result['cache_hit'] = False
    if not result.get('frames'):
        return result
    cache_root.mkdir(parents=True, exist_ok=True)
    staging = cache_root / f'.{key}-{uuid.uuid4().hex}'
    staging.mkdir()
    try:
        names = ['demo.json', 'official.mp4'] + [
            Path(frame).relative_to(directory).as_posix() for frame in result['frames']]
        files = {}
        for name in names:
            source, target = directory / name, staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            files[name] = _sha(target)
        write_json(staging / 'manifest.json',
                   {'key': key, 'files': files, 'frames': names[2:],
                    'duration_seconds': result['duration_seconds'],
                    'trace_seconds': result.get('trace_seconds')})
        try:
            staging.rename(cached)
        except OSError:
            if not cached.is_dir():
                raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return result


def plan(adapter, project, scenarios, requirements, previous, *, backend, model,
         step_frames, max_demos=MAX_DEMOS, uncovered_requirements=None,
         recording_context='initialization'):
    """Ask for complementary demonstrations of the final game's public requirements."""
    visible = [row for row in requirements if row.get('dynamic')] or requirements
    if uncovered_requirements is not None:
        visible = [row for row in visible if row['id'] in uncovered_requirements]
    schema = {'profiles': {'type': 'array', 'minItems': 1, 'maxItems': max_demos,
               'items': {'type': 'object', 'additionalProperties': False,
                         'required': ['id', 'goal', 'scenario', 'requirement_ids',
                                      'expected', 'max_decisions'],
                         'properties': {
                             'id': {'type': 'string'}, 'goal': {'type': 'string'},
                             'scenario': {'type': ['string', 'null']},
                             'requirement_ids': {'type': 'array', 'minItems': 1,
                                                 'items': {'type': 'string'}},
                             'expected': {'type': 'string'},
                             'max_decisions': {'type': 'integer', 'minimum': 1}}}}}
    return models.call(
        'Design FINAL showcase recordings for the completed game. Use public requirements '
        'and the final implementation, not the old three development groups as the profile plan. '
        'Cover as many distinct observable game functions as possible within the supplied clip limit. '
        'Each clip has one coherent goal, a normal title entry (scenario=null) or an EXISTING '
        'launchable scenario ID, and expected visible actions/results. Show normal entry and '
        'core gameplay, progression, important states, feedback and authored animation when '
        'relevant. Use complementary clips rather than duplicate footage. No hidden rubric, '
        'new scenarios, save editing, shortcuts around game rules, or fabricated success. '
        'The existing final-version recordings have ALREADY been checked. Plan ONLY clips '
        'for the supplied uncovered requirements. ' +
        ('RSI core/upgrade recordings occupy the other slots; avoid duplicating their '
         'visible outcomes. RSI upgrade goals describe intended features, not proof that '
         'they were implemented or reached. '
         if recording_context == 'rsi' else
         'Bootstrap scene-floor recordings occupy the remaining slots; avoid duplicating '
         'their visible outcomes. ') +
        'Each decision advances frames_per_decision; max_decisions must not exceed '
        'the supplied limit. Allow time for visible results. '
        'Old group outcomes are route hints only. A requirement that cannot be reached within '
        'the limits may remain uncovered and will be reported honestly.',
        {'task_id': adapter.task_id, 'rules': adapter.instructions,
         'requirements': [{'id': r['id'], 'description': r.get('description', ''),
                           'verification': r.get('verification', '')}
                          for r in visible],
         'existing_scenarios': [{'id': s['id'], 'launch': s.get('launch', 'scenario'),
                                 'purpose': s.get('purpose')} for s in scenarios],
         'previous_group_goals': previous,
         'uncovered_requirement_ids': sorted(uncovered_requirements or []),
         'final_game_source': sources(project),
         'limits': {'max_demos': max_demos, 'max_frames_per_demo': MAX_FRAMES,
                    'frames_per_decision': step_frames,
                    'max_decisions': MAX_FRAMES // step_frames}},
        [], 'final_showcase_plan', schema, tokens=5000, backend=backend, model=model)


def validate_plan(proposal, scenarios, requirements, step_frames, max_demos=MAX_DEMOS):
    rows = proposal.get('profiles') if isinstance(proposal, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= max_demos:
        raise ValueError(f'Final showcase needs 1–{max_demos} planned profiles')
    available = {row['id']: row for row in scenarios}
    normal = next((row for row in scenarios if row.get('launch') == 'default'), None)
    allowed = {row['id'] for row in requirements}
    required = {row['id'] for row in requirements if row.get('dynamic')} or allowed
    if not available or not required:
        raise ValueError('Final showcase needs scenarios and public requirements')
    names, assigned, tasks, plan_rows = set(), set(), [], []
    for row in rows:
        name, scenario = row.get('id'), row.get('scenario')
        ids = row.get('requirement_ids')
        retry_ids = row.get('retry_requirement_ids', ids)
        count = row.get('max_decisions')
        if (not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', name)
                or name in names or (scenario is not None and scenario not in available)
                or (scenario is None and normal is None)
                or not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
                or any(rid not in allowed for rid in ids)
                or not isinstance(retry_ids, list) or not retry_ids
                or any(rid not in ids for rid in retry_ids)
                or type(count) is not int or not 1 <= count <= MAX_FRAMES // step_frames
                or not isinstance(row.get('goal'), str) or not row['goal'].strip()
                or not isinstance(row.get('expected'), str) or not row['expected'].strip()):
            raise ValueError(f'Invalid final showcase profile: {name}')
        names.add(name)
        assigned.update(ids)
        source = normal if scenario is None else available[scenario]
        task = {**source, 'profile': f'showcase/{name}', 'goal': row['goal'],
                'launch': 'default' if scenario is None else 'scenario',
                'max_decisions': count}
        tasks.append(task)
        plan_rows.append({**row, 'scenario_id': source['id'],
                          'normal_launch': scenario is None,
                          'duration_limit_frames': count * step_frames})
    return {'profiles': plan_rows, 'unassigned_requirements': sorted(required - assigned),
            'step_frames': step_frames}, tasks


def validate_baselines(groups, scenarios, project):
    """Reserve one final-version demo for every recorded Bootstrap scene."""
    groups = list(groups or [])
    if len(groups) > MAX_DEMOS:
        raise ValueError('Bootstrap has more scenes than the benchmark demo limit')
    available = {row['id'] for row in scenarios}
    fingerprint = digest(project)
    names = set()
    for group in groups:
        name = group.get('id')
        record = group.get('final_record', {})
        if (not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', name)
                or name in names or not isinstance(group.get('goal'), str)
                or not group['goal'].strip()
                or group.get('scenario') not in available
                or not Path(group.get('bootstrap_video', '')).is_file()
                or record.get('scenario') != group['scenario']
                or bool(record.get('normal_launch')) != bool(group.get('normal_launch'))
                or record.get('project_sha256') != fingerprint
                or not record.get('replay_verified')
                or not Path(record.get('trace', '')).is_file()
                or not Path(record.get('video', '')).is_file()):
            raise ValueError(f'Invalid Bootstrap coverage baseline: {name}')
        names.add(name)
    return groups


def _official_record(project, record, directory, godot, *, demo_name):
    """Replay the actual exported input with the benchmark's OS input runner."""
    benchmark = configure_benchmark()
    if not (benchmark / 'gamecraft_bench/verifier/replay.py').is_file():
        raise RuntimeError(f'Missing GameCraft-Bench replay package: {benchmark}')
    if str(benchmark) not in sys.path:
        sys.path.insert(0, str(benchmark))
    from gamecraft_bench.verifier.replay import replay_trace
    from gamecraft_bench.verifier.score import _sample_frames
    directory.mkdir(parents=True, exist_ok=False)
    project = Path(project)
    import_marker = project / '.godot/showcase-imported'
    if not import_marker.is_file():
        logs = directory / 'logs'
        logs.mkdir()
        import_log = logs / 'import.log'
        with import_log.open('w') as stream:
            imported = subprocess.run(
                [str(godot), '--headless', '--editor', '--import', '--path', str(project)],
                stdout=stream, stderr=subprocess.STDOUT, timeout=180,
                env={**os.environ, 'GODOT_SILENCE_ROOT_WARNING': '1'})
        import_text = import_log.read_text()
        if imported.returncode or 'SCRIPT ERROR' in import_text or 'ERROR:' in import_text:
            raise RuntimeError(f'Godot asset import failed; see {import_log}')
        import_marker.parent.mkdir(parents=True, exist_ok=True)
        import_marker.write_text('ok\n')
    trace = directory / 'demo.json'
    write_json(trace, public_trace(record))
    validate_trace(trace)
    video = directory / 'official.mp4'
    # Keep Godot user saves from other runs and other showcase clips out of
    # this observation. A later clean batch collection remains a separate check.
    userdata = directory / 'userdata'
    userdata.mkdir()
    previous_userdata = os.environ.get('XDG_DATA_HOME')
    try:
        os.environ['XDG_DATA_HOME'] = str(userdata.resolve())
        outcome = replay_trace(project_dir=Path(project), trace_path=trace,
                               output_mp4=video, godot_bin=str(godot),
                               log_dir=directory / 'logs')
    finally:
        if previous_userdata is None:
            os.environ.pop('XDG_DATA_HOME', None)
        else:
            os.environ['XDG_DATA_HOME'] = previous_userdata
    godot_log = directory / 'logs/godot.log'
    if 'ERROR:' in godot_log.read_text():
        raise RuntimeError(f'Godot replay reported errors; see {godot_log}')
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration', '-of', 'json', str(video)],
                           capture_output=True, text=True, check=True, timeout=30)
    video_seconds = float(json.loads(probe.stdout)['format']['duration'])
    if not math.isfinite(video_seconds) or video_seconds <= 0:
        raise RuntimeError('Invalid final showcase video duration')
    frames = _sample_frames(video, directory / 'frames',
                            duration_seconds=video_seconds,
                            interval_seconds=0.5, max_window_seconds=20,
                            seed=demo_name)
    if not video.is_file() or not video.stat().st_size or not frames:
        raise RuntimeError('Final showcase official replay produced no usable video frames')
    return {'trace': str(trace), 'video': str(video),
            'frames': [str(frame) for frame in frames],
            'duration_seconds': video_seconds,
            'trace_seconds': outcome.duration_seconds}


def compare_baseline(group, official, *, backend, model, output, facts=None):
    """Compare visible Bootstrap milestones with one final-game replay."""
    import llm
    from gamecraft_bench.verifier.score import _sample_frames
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    bootstrap = Path(group['bootstrap_video'])
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration', '-of', 'json', str(bootstrap)],
                           capture_output=True, text=True, check=True, timeout=30)
    seconds = float(json.loads(probe.stdout)['format']['duration'])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('Invalid Bootstrap baseline video')
    before = _sample_frames(bootstrap, output / 'bootstrap_frames',
                            duration_seconds=seconds, interval_seconds=0.5,
                            max_window_seconds=20, seed=group['id'])
    after = [Path(path) for path in official['frames'][:40]]
    if not before or not after:
        raise ValueError('Bootstrap coverage comparison needs sampled frames')
    images = []
    for prefix, frames in (('B', before), ('F', after)):
        for index, path in enumerate(frames, 1):
            label = f'{prefix}{index:02d}'
            images.append({'type': 'input_text', 'text': label})
            images.append(llm.img(str(path), width=854, jpeg_quality=80))
    schema = {'facts': {'type': 'array', 'minItems': 1, 'maxItems': 12,
               'items': {'type': 'object', 'additionalProperties': False,
                         'required': ['id', 'description', 'bootstrap_frame',
                                      'final_frame', 'covered'],
                         'properties': {'id': {'type': 'string'},
                                        'description': {'type': 'string'},
                                        'bootstrap_frame': {'type': 'string'},
                                        'final_frame': {'type': ['string', 'null']},
                                        'covered': {'type': 'boolean'}}}}}
    def evaluate(previous_error):
        answer = models.call(
            'Compare actual visible scene coverage, not game code or claimed goals. '
            'B frames are the original Bootstrap recording; F frames are the final-game '
            'recording. On the first comparison, enumerate every distinct goal-relevant '
            'gameplay state, interaction and transition ACTUALLY visible in B, up to 12. '
            'A fact must directly demonstrate the supplied group goal or a visible '
            'prerequisite/result needed to prove that goal. Do not add incidental events '
            'from the recording, such as death, retry, or retained inventory, unless the '
            'group goal explicitly asks to demonstrate them. '
            'Use ids B1, B2, ... and cite a B frame for each. On a retry, evaluate exactly '
            'the supplied fixed facts and ids; do not redefine or omit them. Mark covered '
            'only when an F frame visibly shows the same interaction or resulting state. '
            'A matching scenario name, input action, code path or stated intention is not '
            'visible proof. If uncertain, mark uncovered. Do not assign benchmark scores.',
            {'group': group['id'], 'goal': group['goal'],
             'fixed_baseline_facts': facts, 'previous_validation_error': previous_error,
             'bootstrap_frame_count': len(before), 'final_frame_count': len(after)},
            images, 'bootstrap_scene_coverage', schema, tokens=2800,
            backend=backend, model=model)
        rows = answer.get('facts') if isinstance(answer, dict) else None
        if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
            raise ValueError('Bootstrap comparison returned no visible facts')
        expected = {item['id']: item for item in facts} if facts is not None else None
        if (expected is not None and (len(rows) != len(expected)
                or {item.get('id') for item in rows if isinstance(item, dict)} != set(expected))):
            raise ValueError('Bootstrap retry changed the required visible facts')
        before_labels = {f'B{i:02d}' for i in range(1, len(before) + 1)}
        after_labels = {f'F{i:02d}' for i in range(1, len(after) + 1)}
        normalized = []
        for index, item in enumerate(rows, 1):
            if not isinstance(item, dict):
                raise ValueError('Invalid Bootstrap comparison fact')
            identity = item.get('id') if expected is not None else f'B{index}'
            description = (expected[identity]['description'] if expected is not None
                           else item.get('description'))
            baseline_frame = (expected[identity]['bootstrap_frame'] if expected is not None
                              else item.get('bootstrap_frame'))
            if (not isinstance(description, str) or not description.strip()
                    or baseline_frame not in before_labels):
                raise ValueError('Bootstrap comparison has no valid baseline evidence')
            final_frame = item.get('final_frame')
            covered = item.get('covered') is True and final_frame in after_labels
            normalized.append({'id': identity, 'description': description,
                               'bootstrap_frame': baseline_frame,
                               'final_frame': final_frame if covered else None,
                               'covered': covered})
        report = {'group': group['id'], 'goal': group['goal'],
                  'bootstrap_video': str(bootstrap), 'final_video': official['video'],
                  'facts': normalized, 'passed': all(item['covered'] for item in normalized)}
        write_json(output / 'comparison.json', report)
        return report
    previous_error = None
    for _ in range(2):
        try:
            return evaluate(previous_error)
        except ValueError as exc:
            previous_error = str(exc)
    raise ValueError("Bootstrap comparison invalid after one retry: " + previous_error)


def assess(row, official, *, backend, model, requirements=None, feedback=None):
    """Judge only whether planned public interactions appear in actual replay frames."""
    import llm
    frames = [Path(path) for path in official['frames']]
    # Inspect the same half-second sampling cadence used by benchmark replay.
    shown = frames[:40]
    images = []
    for path in shown:
        images.append({'type': 'input_text', 'text': path.name})
        images.append(llm.img(str(path), width=854, jpeg_quality=85))
    schema = {'coverage': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['id', 'status', 'reason', 'evidence'],
        'properties': {'id': {'type': 'string'},
                       'status': {'type': 'string', 'enum':
                                  ['shown', 'partial', 'not_shown', 'unverified']},
                       'reason': {'type': 'string'},
                       'evidence': {'type': 'array', 'items': {'type': 'string'}}}}}}
    answer = models.call(
        'Assess the FINAL official OS-input replay, using only the supplied actual frames. '
        'For each assigned public requirement, say whether its requested interaction AND '
        'visible effect are shown, partially shown, not shown, or unverified. A plan, code '
        'path, valid trace, or claimed policy goal is not visual evidence. Cite only supplied '
        'frame filenames. Missing coverage is not automatically a game defect; identify when '
        'the player simply did not reach the state. Do not assign benchmark scores.',
        {'profile': row,
         'assigned_requirements': [
             {'id': item['id'], 'description': item.get('description', ''),
              'verification': item.get('verification', '')}
             for item in (requirements or []) if item['id'] in row['requirement_ids']],
         'previous_feedback': feedback,
         'frame_count': len(frames), 'duration_seconds': official['duration_seconds']},
        images, 'final_showcase_coverage', schema, tokens=2000,
        backend=backend, model=model)
    found = answer.get('coverage') if isinstance(answer, dict) else None
    if (not isinstance(found, list) or len(found) != len(row['requirement_ids'])
            or {item.get('id') for item in found if isinstance(item, dict)} !=
            set(row['requirement_ids'])):
        raise ValueError('Final showcase coverage assessment omitted planned requirements')
    allowed = {path.name for path in shown}
    for item in found:
        if (item.get('status') not in {'shown', 'partial', 'not_shown', 'unverified'}
                or not isinstance(item.get('reason'), str)
                or not isinstance(item.get('evidence'), list)):
            raise ValueError('Final showcase assessment cited invalid visual evidence')
        item['evidence'] = [name for name in item['evidence']
                            if isinstance(name, str) and name in allowed]
        if item['status'] in {'shown', 'partial'} and not item['evidence']:
            item['status'] = 'unverified'
            item['reason'] += ' (No valid supplied frame cited.)'
    return found


def _shown_count(coverage):
    return sum({'shown': 2, 'partial': 1}.get(item['status'], 0) for item in coverage)


def _covering_baseline_candidates(candidates):
    """Choose the fewest clips whose observed facts cover the Bootstrap goal."""
    facts = candidates[0]['comparison']['facts']
    for count in range(1, len(candidates) + 1):
        for indices in combinations(reversed(range(len(candidates))), count):
            chosen = [candidates[index] for index in indices]
            if all(any(any(item['id'] == fact['id'] and item['covered']
                           for item in candidate['comparison']['facts'])
                       for candidate in chosen) for fact in facts):
                return sorted(chosen, key=candidates.index)
    return None


def _best_effort_baseline_candidates(candidates):
    """Keep the strongest observed evidence when a Bootstrap fact is unreachable."""
    facts = {fact['id'] for fact in candidates[0]['comparison']['facts']}
    best, best_key = None, None
    for count in range(1, len(candidates) + 1):
        for indices in combinations(range(len(candidates)), count):
            covered = {fact['id'] for index in indices
                       for fact in candidates[index]['comparison']['facts']
                       if fact['covered']}
            key = (len(covered & facts), -count, tuple(reversed(indices)))
            if best_key is None or key > best_key:
                best, best_key = indices, key
    return [candidates[index] for index in best]


def _record_profiles(project, output, show_plan, tasks, *, replay_project, fingerprint,
                     cache, requirements, skill, adapter, rollout, godot, model,
                     backend, step_frames, policy_factory, assessor, prefix=''):
    """Record a profile batch with one coverage-guided retry per profile."""
    selected, attempts, assessments = [], [], []
    profiles = profiles_for(tasks)
    original = play_version(project, tasks, output / f'{prefix}attempt-01', profiles=profiles,
        skill=skill, reuse=False,
        context={'phase': 'final_showcase', 'showcase_plan': show_plan,
                 'behavior_skill_enabled': bool(skill)},
        adapter=adapter, rollout=rollout, godot=godot, model=model, backend=backend,
        step_frames=step_frames, max_decisions=MAX_FRAMES // step_frames,
        factory=policy_factory) if tasks else []
    planned_rows = list(show_plan['profiles'])
    for index, (row, task, record) in enumerate(zip(planned_rows, tasks, original)):
        demo_name = f'{index + 1:02d}_{row["id"]}'
        first = _cached_official_record(replay_project, record,
            output / 'official' / f'{prefix}{index + 1:02d}-attempt-01', godot,
            demo_name=demo_name, project_hash=fingerprint, cache_root=cache)
        coverage = assessor(row, first, backend=backend, model=model,
                            requirements=requirements)
        choices = [{'record': record, 'official': first, 'coverage': coverage}]
        retry_ids = row.get('retry_requirement_ids', row['requirement_ids'])
        goal_coverage = [item for item in coverage if item['id'] in retry_ids]
        if any(item['status'] != 'shown' for item in goal_coverage):
            feedback = {'coverage': goal_coverage, 'expected': row['expected']}
            retry = play_version(project, [task], output / f'{prefix}retry-{index + 1:02d}',
                profiles=[profiles[index]], prior=[record], reuse=False, skill=skill,
                context={'phase': 'final_showcase', 'showcase_plan': {'profiles': [row]},
                         'coverage_feedback': feedback,
                         'behavior_skill_enabled': bool(skill)},
                adapter=adapter, rollout=rollout, godot=godot, model=model,
                backend=backend, step_frames=step_frames,
                max_decisions=MAX_FRAMES // step_frames, factory=policy_factory)[0]
            second = _cached_official_record(replay_project, retry,
                output / 'official' / f'{prefix}{index + 1:02d}-attempt-02', godot,
                demo_name=demo_name, project_hash=fingerprint, cache_root=cache)
            choices.append({'record': retry, 'official': second,
                            'coverage': assessor(row, second, backend=backend,
                                                 model=model, requirements=requirements,
                                                 feedback=feedback)})
        def selection_key(index):
            items = choices[index]['coverage']
            return (_shown_count([item for item in items if item['id'] in retry_ids]),
                    _shown_count(items))
        chosen = max(range(len(choices)), key=selection_key)
        selected.append({'row': row, **choices[chosen]})
        attempts.append({'profile': row['id'], 'chosen_attempt': chosen + 1,
                         'attempts': [{'video': c['official']['video'],
                                      'coverage': c['coverage']} for c in choices]})
        assessments.extend({'profile': row['id'], **item} for item in choices[chosen]['coverage'])
    return selected, attempts, assessments


def run(project, output, *, adapter, scenarios, requirements, previous, skill,
        godot, model, backend, step_frames, policy_factory, rollout,
        planner=plan, assessor=assess, baseline_groups=(),
        baseline_comparator=compare_baseline, initial_profiles=None,
        supplemental=True):
    """Record independent final clips; preserve all attempts and chosen evidence."""
    project, output = Path(project), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    fingerprint = digest(project)
    baseline_groups = validate_baselines(baseline_groups, scenarios, project)
    seed_plan, seed_tasks = (validate_plan(initial_profiles, scenarios, requirements,
                                         step_frames) if initial_profiles is not None
                             else (None, []))
    if seed_plan and baseline_groups:
        raise ValueError('Use either Bootstrap baselines or fresh initial profiles')
    # Official replay may import Godot resources; keep the selected game immutable.
    replay_project = output / 'replay-project'
    shutil.copytree(project, replay_project,
                    ignore=shutil.ignore_patterns('.godot', '.git', '__pycache__'))
    cache = output.parent / '.final_showcase_replay_cache'
    required = {row['id'] for row in requirements if row.get('dynamic')} or {
        row['id'] for row in requirements}
    selected, assessments, attempts, baseline_reports = [], [], [], []
    # Examine the final three-group evidence before spending any Player calls
    # on new clips. Bootstrap floor repair takes priority over extras.
    for offset, group in enumerate(baseline_groups, 1):
        profile = f'bootstrap_{group["id"]}'
        row = {'id': profile, 'goal': group['goal'],
               'scenario': None if group['normal_launch'] else group['scenario'],
               'scenario_id': group['scenario'],
               'normal_launch': bool(group['normal_launch']),
               'requirement_ids': sorted(required),
               'expected': 'Preserve visible Bootstrap coverage and show public functions.',
               'max_decisions': MAX_FRAMES // step_frames,
               'duration_limit_frames': MAX_FRAMES,
               'bootstrap_group': group['id']}
        record = group['final_record']
        first = _cached_official_record(replay_project, record,
            output / 'official' / f'{offset:02d}-bootstrap-01', godot,
            demo_name=group['id'], project_hash=fingerprint, cache_root=cache)
        comparison = baseline_comparator(group, first, backend=backend, model=model,
            output=output / 'bootstrap_coverage' / f'{group["id"]}-attempt-01')
        candidates = [{'record': record, 'official': first, 'comparison': comparison}]
        for attempt in (2, 3):
            if _covering_baseline_candidates(candidates):
                break
            missing = [fact for fact in comparison['facts']
                       if not any(any(item['id'] == fact['id'] and item['covered']
                                      for item in candidate['comparison']['facts'])
                                  for candidate in candidates)]
            if not missing:
                break
            task = {'id': group['scenario'],
                    'launch': 'default' if group['normal_launch'] else 'scenario',
                    'profile': f'showcase/{profile}',
                    'goal': group['goal'] + ' Show these missing Bootstrap moments: ' +
                            '; '.join(item['description'] for item in missing) +
                            (' Pause at least 36 frames after the missing visible change '
                             'before advancing to another result screen.' if attempt == 3 else ''),
                    'max_decisions': MAX_FRAMES // step_frames}
            retry = play_version(project, [task],
                output / f'bootstrap-retry-{group["id"]}-{attempt - 1:02d}',
                profiles=profiles_for([task]), prior=[candidates[-1]['record']],
                reuse=False, skill=skill,
                context={'phase': 'final_showcase', 'bootstrap_scene_floor': group['goal'],
                         'coverage_feedback': missing,
                         'behavior_skill_enabled': bool(skill)},
                adapter=adapter, rollout=rollout, godot=godot, model=model,
                backend=backend, step_frames=step_frames,
                max_decisions=MAX_FRAMES // step_frames, factory=policy_factory)[0]
            official = _cached_official_record(replay_project, retry,
                output / 'official' / f'{offset:02d}-bootstrap-{attempt:02d}', godot,
                demo_name=group['id'], project_hash=fingerprint, cache_root=cache)
            candidates.append({'record': retry, 'official': official,
                'comparison': baseline_comparator(group, official, backend=backend,
                    model=model, output=output / 'bootstrap_coverage' /
                    f'{group["id"]}-attempt-{attempt:02d}', facts=comparison['facts'])})
        passing = _covering_baseline_candidates(candidates)
        floor_passed = passing is not None
        if passing is None:
            passing = _best_effort_baseline_candidates(candidates)
        missing_fact_ids = sorted({fact['id'] for fact in comparison['facts']
            if not any(any(item['id'] == fact['id'] and item['covered']
                           for item in candidate['comparison']['facts'])
                       for candidate in passing)})
        baseline_reports.append({'group': group['id'], 'scenario': group['scenario'],
                                 'attempts': [item['comparison'] for item in candidates],
                                 'selected_attempts': [candidates.index(item) + 1
                                                      for item in passing],
                                 'passed': floor_passed,
                                 'missing_fact_ids': missing_fact_ids})
        write_json(output / 'bootstrap_coverage.json', baseline_reports)
        if len(selected) + len(passing) > MAX_DEMOS:
            raise RuntimeError('Bootstrap coverage needs more clips than the benchmark allows')
        for chosen_index, item in enumerate(passing, 1):
            chosen_row = dict(row)
            if len(passing) > 1:
                chosen_row['id'] = f'{profile}_{chosen_index:02d}'
            coverage = assessor(chosen_row, item['official'], backend=backend,
                                model=model, requirements=requirements)
            selected.append({'row': chosen_row, 'record': item['record'],
                             'official': item['official'], 'coverage': coverage})
            assessments.extend({'profile': chosen_row['id'], **fact} for fact in coverage)
        attempts.append({'profile': profile,
                         'chosen_attempt': (candidates.index(passing[0]) + 1
                                            if len(passing) == 1 else None),
                         'chosen_attempts': [candidates.index(item) + 1 for item in passing],
                         'attempts': [{'video': item['official']['video'],
                                      'bootstrap_comparison': item['comparison']}
                                     for item in candidates]})
    if seed_plan:
        write_json(output / 'initial_profiles.json', seed_plan)
        seed_selected, seed_attempts, seed_assessments = _record_profiles(
            project, output, seed_plan, seed_tasks, replay_project=replay_project,
            fingerprint=fingerprint, cache=cache, requirements=requirements, skill=skill,
            adapter=adapter, rollout=rollout, godot=godot, model=model, backend=backend,
            step_frames=step_frames, policy_factory=policy_factory, assessor=assessor,
            prefix='seed-')
        selected.extend(seed_selected)
        attempts.extend(seed_attempts)
        assessments.extend(seed_assessments)
    covered = {item['id'] for item in assessments if item['status'] == 'shown'}
    uncovered = required - covered
    available_slots = MAX_DEMOS - len(selected)
    prior_evidence = previous + [{'profile': choice['row']['id'],
        'visible_coverage': choice['coverage']} for choice in selected]
    if supplemental and uncovered and available_slots:
        planner_options = {'backend': backend, 'model': model,
                           'step_frames': step_frames,
                           'max_demos': available_slots,
                           'uncovered_requirements': uncovered}
        proposal = planner(adapter, project, scenarios,
                           [row for row in requirements if row['id'] in uncovered], prior_evidence,
                           **planner_options)
        show_plan, tasks = validate_plan(proposal, scenarios,
                                         [row for row in requirements if row['id'] in uncovered],
                                         step_frames, max_demos=available_slots)
    else:
        show_plan = {'profiles': [], 'unassigned_requirements': sorted(uncovered),
                     'step_frames': step_frames}
        tasks = []
    if any(row['id'] in {choice['row']['id'] for choice in selected}
           for row in show_plan['profiles']):
        raise ValueError('Final showcase profile collides with a retained profile')
    show_plan['bootstrap_scene_floor'] = [
        {'group': row['id'], 'scenario': row['scenario'],
         'normal_launch': bool(row['normal_launch'])} for row in baseline_groups]
    write_json(output / 'plan.json', show_plan)
    extra_selected, extra_attempts, extra_assessments = _record_profiles(
        project, output, show_plan, tasks, replay_project=replay_project,
        fingerprint=fingerprint, cache=cache, requirements=requirements, skill=skill,
        adapter=adapter, rollout=rollout, godot=godot, model=model, backend=backend,
        step_frames=step_frames, policy_factory=policy_factory, assessor=assessor)
    selected.extend(extra_selected)
    attempts.extend(extra_attempts)
    assessments.extend(extra_assessments)
    show_plan['profiles'] = [choice['row'] for choice in selected]
    write_json(output / 'plan.json', show_plan)
    if digest(project) != fingerprint:
        raise RuntimeError('Final game changed during showcase recording')
    demos = output / 'demo_outputs'
    demos.mkdir()
    entries = []
    for index, (row, choice) in enumerate(zip(show_plan['profiles'], selected), 1):
        record, official = choice['record'], choice['official']
        if record['project_sha256'] != fingerprint or not record['replay_verified']:
            raise ValueError('Showcase trace is not verified on the final game')
        dest = demos / f'{index:02d}_{row["id"]}.json'
        write_json(dest, public_trace(record))
        validate_trace(dest)
        if _sha(dest) != _sha(official['trace']):
            raise ValueError('Exported showcase demo differs from scored OS-input replay')
        entries.append({'name': dest.name, 'profile': row['id'], 'scenario': record['scenario'],
                        'trace_sha256': _sha(dest), 'source_trace': record['trace'],
                        'source_video': record['video'],
                        'source_policy': record['player_policy'],
                        'official_video': official['video'],
                        'official_video_sha256': _sha(official['video']),
                        'coverage': choice['coverage']})
    rank = {'not_planned': 0, 'not_shown': 1, 'unverified': 2,
            'partial': 3, 'shown': 4}
    requirement_coverage = {}
    for item in requirements:
        if item.get('dynamic') or not any(r.get('dynamic') for r in requirements):
            requirement_coverage[item['id']] = {'status': 'not_planned', 'profiles': []}
    for item in assessments:
        summary = requirement_coverage.setdefault(item['id'],
            {'status': 'not_planned', 'profiles': []})
        summary['profiles'].append(item['profile'])
        if rank[item['status']] > rank[summary['status']]:
            summary['status'] = item['status']
    manifest = {'status': 'official_replays_collected', 'project_sha256': fingerprint,
                'uses_behavior_skill': bool(skill),
                'behavior_skill_sha256': hashlib.sha256(skill.encode()).hexdigest() if skill else None,
                'demos': entries, 'unassigned_requirements': show_plan['unassigned_requirements'],
                'coverage': assessments, 'requirement_coverage': requirement_coverage,
                'bootstrap_scene_floor_passed': bool(baseline_groups)
                    and all(row['passed'] for row in baseline_reports),
                'bootstrap_coverage': baseline_reports,
                'attempts': attempts,
                'scope': 'Final game, actual Player inputs, official OS-input replay videos. '
                         'Coverage is a visual evidence assessment, not a benchmark score or '
                         'independent proof that every public requirement is implemented. '
                         'A separate clean BUILD and replay collection is still required for '
                         'a complete score-project benchmark pass.'}
    if seed_plan:
        manifest['initial_profiles'] = [row['id'] for row in seed_plan['profiles']]
        manifest['bootstrap_scene_floor_passed'] = None
    write_json(output / 'manifest.json', manifest)
    return manifest


def archive(source, target, manifest):
    """Keep portable showcase evidence without copying every runtime game workspace."""
    source, target = Path(source), Path(target)
    target.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source / 'plan.json', target / 'plan.json')
    shutil.copytree(source / 'official', target / 'official',
                    ignore=shutil.ignore_patterns('userdata'))
    policies = target / 'policies'
    policies.mkdir()
    videos = target / 'videos'
    videos.mkdir()
    for index, entry in enumerate(manifest['demos'], 1):
        video = Path(entry['official_video'])
        if not video.is_relative_to(source) or not video.is_file():
            raise ValueError('Showcase video escaped source archive')
        relative = video.relative_to(source)
        archived_video = videos / f'{index:02d}_{entry["profile"]}.mp4'
        shutil.copyfile(video, archived_video)
        if _sha(archived_video) != entry['official_video_sha256']:
            raise ValueError('Archived showcase video checksum mismatch')
        policy = Path(entry['source_policy'])
        if not policy.is_file():
            raise ValueError('Missing selected showcase policy')
        archived_policy = policies / f'{index:02d}_{entry["profile"]}.py'
        shutil.copyfile(policy, archived_policy)
        entry['archived_video'] = archived_video.relative_to(target).as_posix()
        entry['replay_evidence'] = relative.parent.as_posix()
        entry['archived_policy'] = archived_policy.relative_to(target).as_posix()
        entry['archived_policy_sha256'] = _sha(archived_policy)
    write_json(target / 'manifest.json', manifest)
