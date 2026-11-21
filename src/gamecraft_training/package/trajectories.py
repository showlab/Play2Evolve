"""Version-owned initialization scripts, authored after a candidate is built."""
import copy
import json
import math
import os
from pathlib import Path

from gamecraft_behavior.core import digest, write_json


def script_action(adapter, step):
    if hasattr(adapter, 'scripted_action'):
        return adapter.scripted_action(step)
    # Some policy test doubles implement the older key-only adapter interface.
    value = {'action': step, 'reason': ''}
    adapter.validate_action(value)
    return value


def author(config, adapter, project, tasks, diagnosis, changes, episodes, feedback):
    from . import models
    from gamecraft_composition.core import sources
    requirements = config.get('_initialization_scene', {}).get('requirements', [])
    if not requirements and config.get('_package_root'):
        path = Path(config['_package_root']) / 'requirements.json'
        if path.is_file():
            requirements = json.loads(path.read_text()).get('requirements', [])
    action_item = {'type': 'string', 'enum': list(adapter.actions)}
    if getattr(adapter, 'pointer_actions', ()):
        pointer = adapter.action_schema['properties']['pointer']['anyOf'][1]
        action_item = {'anyOf': [action_item,
            {'type': 'object', 'additionalProperties': False, 'required': ['action', 'pointer'],
             'properties': {'action': {'type': 'string', 'enum': list(adapter.pointer_actions)},
                            'pointer': pointer}}]}
    row = {'type': 'object', 'additionalProperties': False,
           'required': ['id', 'goal', 'reason', 'step_frames', 'actions', 'checks'],
           'properties': {
               'id': {'type': 'string', 'enum': [task['id'] for task in tasks]},
               'goal': {'type': 'string'}, 'reason': {'type': 'string'},
               'step_frames': {'type': 'integer', 'minimum': 1, 'maximum': 30},
               'actions': {'type': 'array', 'minItems': 1, 'maxItems': 600,
                           'items': action_item},
               'checks': {'type': 'array', 'minItems': 1, 'maxItems': 6,
                          'items': {'type': 'object', 'additionalProperties': False,
                                    'required': ['field', 'op', 'value', 'when'],
                                    'properties': {
                                        'field': {'type': 'string'},
                                        'op': {'type': 'string', 'enum': ['eq', 'gte', 'lte']},
                                        'value': {'type': ['string', 'number', 'boolean']},
                                        'when': {'type': 'string', 'enum': ['ever', 'final']}}}}}}
    return models.call(
        'Design fixed playtest inputs for this completed candidate game. Read its code and the old scripts, '
        'observations and repair changes. Return one script for EVERY supplied scenario, preserving its purpose '
        'and launch state. Demonstrate the original required interactions AND the proposed repair with readable '
        'lead-in, action, feedback and aftermath. Adapt timing to the actual candidate; keeping valid old actions '
        'is allowed. Do not avoid a required difficult interaction just to make the candidate look better. '
        'Use only allowed semantic action IDs. For pointer-enabled games, a pointer action '
        'is an object with action and normalized pointer x,y,to_x,to_y; key actions remain strings. '
        'Each action advances step_frames physics frames; the entire '
        'script must fit 600 frames. Do not change the game, rules, seed, scenario IDs or launch mode. '
        'The host will execute these inputs; goals and reasons are intentions, not evidence of success. '
        'For each scenario, provide concrete checks on observable game-state fields for its '
        'essential progression and reward (such as training_score >= 3, power >= 2, or '
        'state ever equals fight). Use fields in the supplied observations or candidate code; '
        'do not use trivial replay/terminal checks or invent fields. The host checks these '
        'after replay. If feedback reports unmet checks, use the actual final state and '
        'action history to correct ONLY the failed scenario. Visual repair still needs A/B review.',
        {'rules': adapter.instructions, 'old_scripts': tasks, 'diagnosis': diagnosis,
         'candidate_changes': changes, 'candidate_code': sources(project),
         'active_requirements': requirements,
         'previous_observations': models._runtime_trace(episodes), 'feedback': feedback},
        models.content(episodes, limit=12, scene_focus=True), 'initialization_trajectories',
        {'scenarios': {'type': 'array', 'minItems': len(tasks), 'maxItems': len(tasks), 'items': row}},
        tokens=6000, backend='codex',
        model=os.environ.get('GAMECRAFT_TRAJECTORY_MODEL',
                             os.environ.get('GAMECRAFT_REPAIR_PLANNER_MODEL', 'gpt-6-sol')))


def validate(proposal, tasks, adapter):
    if not isinstance(proposal, dict) or set(proposal) != {'scenarios'}:
        raise ValueError('Expected a trajectory proposal with scenarios')
    rows = proposal.get('scenarios')
    ids = [task['id'] for task in tasks]
    if (not isinstance(rows, list) or len(rows) != len(ids)
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in rows)
            or sorted(row.get('id', '') for row in rows) != sorted(ids)):
        raise ValueError('Trajectory proposal must cover every active scenario exactly once')
    by_id = {row['id']: row for row in rows}
    result = []
    for task in tasks:
        row = by_id[task['id']]
        if set(row) not in ({'id', 'goal', 'reason', 'step_frames', 'actions'},
                            {'id', 'goal', 'reason', 'step_frames', 'actions', 'checks'}):
            raise ValueError('Trajectory Agent may only change actions and timing')
        if any(not isinstance(row[key], str) or not row[key].strip() for key in ('goal', 'reason')):
            raise ValueError('Trajectory needs a goal and a reason')
        step, actions = row['step_frames'], row['actions']
        if (type(step) is not int or not 1 <= step <= 30 or not isinstance(actions, list)
                or not actions or len(actions) * step > 600):
            raise ValueError('Trajectory must fit the 600-frame replay budget')
        for action in actions:
            script_action(adapter, action)
        if 'checks' in row:
            checks = row['checks']
            if not isinstance(checks, list) or not 1 <= len(checks) <= 6:
                raise ValueError('Trajectory needs 1–6 observable goal checks')
            for check in checks:
                if (not isinstance(check, dict)
                        or set(check) != {'field', 'op', 'value', 'when'}
                        or not isinstance(check['field'], str) or not check['field']
                        or check['op'] not in ('eq', 'gte', 'lte')
                        or check['when'] not in ('ever', 'final')
                        or type(check['value']) not in (str, int, float, bool)
                        or (type(check['value']) is float and not math.isfinite(check['value']))
                        or (check['op'] != 'eq' and type(check['value']) not in (int, float))):
                    raise ValueError('Invalid observable trajectory goal check')
        result.append({**copy.deepcopy(task), 'step_frames': step, 'actions': list(actions)})
    return result


def check_goals(run, checks):
    """Check declared, machine-observable milestones; visuals remain for A/B Judge."""
    if checks is None:
        return {'status': 'uncheckable', 'checks': [], 'failed': []}
    states = [d.get('evaluation_state', {}) for d in run.get('decisions', [])]
    states.append(run.get('replay', {}))
    outcomes = []
    for check in checks:
        observed = [state[check['field']] for state in
                    (states if check['when'] == 'ever' else states[-1:])
                    if isinstance(state, dict) and check['field'] in state]
        def matches(value):
            target = check['value']
            if check['op'] == 'eq':
                return type(value) is type(target) and value == target
            return (type(value) in (int, float) and math.isfinite(value)
                    and (value >= target if check['op'] == 'gte' else value <= target))
        passed = any(matches(value) for value in observed)
        outcomes.append({**check, 'passed': passed,
                         'final_observed': observed[-1] if observed else None})
    return {'status': 'passed' if all(c['passed'] for c in outcomes) else 'failed',
            'checks': outcomes, 'failed': [c for c in outcomes if not c['passed']]}


def replay_candidate(config, adapter, project, episodes, output, *, diagnosis, changes,
                     play, propose=author, attempts=2):
    """Author and execute privately. Only the caller may promote the resulting scripts."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    tasks = [copy.deepcopy(episode['scripted_case']) for episode in episodes]
    order = [task['id'] for task in tasks]
    if len(set(order)) != len(order):
        raise ValueError('Candidate trajectory needs unique scenarios')
    originals = {episode['scenario']: episode for episode in episodes}
    original = digest(project)
    feedback = []
    selected = {}
    selected_tasks = {}
    required_checks = {}
    pending = set(order)
    for index in range(attempts):
        directory = output / f'attempt-{index + 1:02}'
        directory.mkdir()
        try:
            active = [task for task in tasks if task['id'] in pending]
            proposal = propose(config, adapter, project, copy.deepcopy(active), diagnosis,
                               changes, [copy.deepcopy(selected.get(t['id'], originals[t['id']]))
                                         for t in active],
                               copy.deepcopy(feedback))
            write_json(directory / 'proposal.json', proposal)
            proposed = validate(proposal, active, adapter)
            runs = play(project, proposed, directory / 'replay')
            if (len(runs) != len(proposed) or any(
                    not run.get('replay_verified') or not run.get('decisions')
                    or run.get('scenario') != task['id']
                    or run.get('project_sha256') != original
                    or bool(run.get('normal_launch')) != (task.get('launch', 'scenario') == 'default')
                    or run.get('decision_frames') != task['step_frames']
                    or [{k:d[k] for k in ('action','pointer') if k in d} for d in run['decisions']] !=
                       [{k:d[k] for k in ('action','pointer') if k in d} for d in
                        (script_action(adapter, a) for a in task['actions'][:len(run['decisions'])])]
                    or len(run['decisions']) > len(task['actions'])
                    or any(not run.get(key) or not Path(run[key]).is_file()
                           or not Path(run[key]).stat().st_size for key in ('trace', 'video'))
                    for run, task in zip(runs, proposed))):
                raise ValueError('Candidate trajectory did not produce complete verified episode evidence')
            if digest(project) != original:
                raise RuntimeError('Trajectory author or replay modified the candidate game')
            details = {row['id']: row for row in proposal['scenarios']}
            coverage = {}
            for run, task in zip(runs, proposed):
                required_checks.setdefault(task['id'], copy.deepcopy(details[task['id']].get('checks')))
                run['scripted_case'] = copy.deepcopy(task)
                run['trajectory'] = {'proposal': str(directory / 'proposal.json'),
                                     'goal': details[task['id']]['goal'],
                                     'reason': details[task['id']]['reason']}
                run['goal_coverage'] = check_goals(run, required_checks[task['id']])
                selected[task['id']] = run
                selected_tasks[task['id']] = task
                coverage[task['id']] = run['goal_coverage']
            write_json(directory / 'goal_coverage.json', coverage)
            pending = {task_id for task_id in order
                       if selected.get(task_id, {}).get('goal_coverage', {}).get('status') == 'failed'}
            if pending and index + 1 < attempts:
                failure = {'attempt': index + 1, 'kind': 'goal_not_met',
                           'failed_scenarios': sorted(pending),
                           'observed': {task_id: {
                               'failed_checks': selected[task_id]['goal_coverage']['failed'],
                               'final_state': selected[task_id].get('replay', {}),
                               'actions': [d['action'] for d in selected[task_id]['decisions']]}
                               for task_id in sorted(pending)}}
                write_json(directory / 'feedback.json', failure)
                feedback.append(failure)
                continue
            break
        except (ValueError, RuntimeError, OSError) as exc:
            if digest(project) != original:
                raise RuntimeError('Trajectory stage changed the candidate game') from exc
            failure = {'attempt': index + 1, 'error': f'{type(exc).__name__}: {exc}'}
            write_json(directory / 'failure.json', failure)
            feedback.append(failure)
    if len(selected) != len(order):
        write_json(output / 'result.json', {'status': 'failed', 'attempts': feedback})
        raise RuntimeError('Candidate trajectory unavailable: ' + feedback[-1]['error'])
    runs = [selected[task_id] for task_id in order]
    status = ('verified_goal_incomplete' if pending else 'verified')
    write_json(output / 'result.json', {'status': status, 'project_sha256': original,
               'scenarios': [selected_tasks[task_id] for task_id in order], 'episodes': runs,
               'goal_coverage': {task_id: selected[task_id]['goal_coverage'] for task_id in order},
               'attempts': feedback,
               'scope': 'Execution verified; goal checks cover observable milestones only. '
                        'The A/B Judge assesses visible repair and any missing coverage.'})
    return runs


def adopt(cases, episodes):
    """Update shared schedule references only from selected, verified evidence."""
    by_id = {case['id']: case for case in cases}
    updates = []
    for episode in episodes:
        case = episode['scripted_case']
        if not episode.get('replay_verified') or case['id'] != episode['scenario']:
            raise ValueError('Cannot adopt an unverified or mismatched trajectory')
        current = by_id[case['id']]
        if {k: v for k, v in current.items() if k not in ('actions', 'step_frames')} != {
                k: v for k, v in case.items() if k not in ('actions', 'step_frames')}:
            raise ValueError('Trajectory adoption changed scenario identity or purpose')
        updates.append((current, copy.deepcopy(case)))
    for current, case in updates:
        current.clear()
        current.update(case)


def export_demos(project, episodes, output, *, max_demos=10):
    """Export final recorded inputs. Native benchmark collection is still required."""
    from gamecraft_evaluation.trained_demos import public_trace
    from gamecraft_evaluation.packaging import validate_trace
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    project_hash = digest(project)
    entries = []
    for index, episode in enumerate(episodes):
        if not episode.get('replay_verified') or episode.get('project_sha256') != project_hash:
            raise ValueError('Final demo evidence does not belong to the final game')
        path = output / f'{index + 1:02}_initialization.json'
        write_json(path, public_trace(episode))
        validate_trace(path)
        entries.append({'file': path.name, 'scenario': episode['scenario'],
                        'source_trace': episode['trace'], 'source_video': episode.get('video')})
    if not entries:
        raise ValueError('Final initialization has no demo evidence')
    return {'status': 'collection_required' if len(entries) <= max_demos else 'demo_limit_exceeded',
            'demos': entries, 'max_demos': max_demos,
            'scope': 'Final initialization scenarios; original demos remain in the initial package. '
                     'Frame replay is verified. Clean OS-input benchmark collection is not yet verified.'}


def export_declared_demos(cases, adapter, output, *, max_demos=10):
    """Save declared inputs for later RSI/benchmark work without executing them."""
    from gamecraft_behavior.core import trace
    from gamecraft_evaluation.packaging import validate_trace
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    active = [case for case in cases if case['purpose'] == 'initialization']
    entries = []
    for index, case in enumerate(active[:max_demos], 1):
        step = case.get('step_frames', 6)
        decisions = []
        for i, action in enumerate(case['actions']):
            event = {'action': action} if isinstance(action, str) else dict(action)
            decisions.append({**event, 'frame': i * step,
                              'reason': 'Declared initialization input'})
        data = trace(decisions, len(decisions) * step, adapter)
        # The live Player uses a single mouse_drag event. Benchmark demo traces
        # use the portable OS-input down/move/up sequence instead.
        events = []
        for event in data['events']:
            if event['type'] == 'mouse_drag':
                frame = event['frame']
                button = event.get('button', 'left')
                events.extend((
                    {'frame': frame, 'type': 'mouse_down', 'button': button,
                     'x': event['x'], 'y': event['y']},
                    {'frame': frame, 'type': 'mouse_move',
                     'x': event['to_x'], 'y': event['to_y']},
                    {'frame': frame, 'type': 'mouse_up', 'button': button,
                     'x': event['to_x'], 'y': event['to_y']},
                ))
            else:
                events.append(event)
        data['events'] = events
        if case.get('launch', 'scenario') != 'default':
            data['scenario'] = case['id']
        else:
            data.pop('scenario', None)
        path = output / f'{index:02}_initialization.json'
        write_json(path, data)
        validate_trace(path)
        entries.append({'file': path.name, 'scenario': case['id'], 'replay_verified': False})
    return {'status': 'declared_unverified', 'demos': entries, 'max_demos': max_demos,
            'omitted_scenarios': [case['id'] for case in active[max_demos:]],
            'scope': 'Declared inputs only; no final-version replay or quality acceptance was run.'}
