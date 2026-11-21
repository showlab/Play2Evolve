from __future__ import annotations
import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

from .core import HeldKeyState, digest, prepare, trace, write_json

from .adapters import resolve
# Compatibility aliases for callers that inspect the original bird prompt/schema.
from .adapters.dread_wings import SYSTEM
ACT = resolve().action_schema


def player(model='local', backend='local', *, adapter=None, run_objective=None, previous_playtest=None):
    adapter = resolve(adapter=adapter)
    from .objective import COMPLETION_PROMPT, action_schema, decision
    def decide(image, skill, recent, step_frames, *, current_state=None):
        import llm
        controls = {name: ('wait' if key is None else f'press {key}')
                    for name, key in adapter.actions.items()}
        controls.update({name: f'{kind} {key}' for name, (kind, key) in adapter.key_actions.items()})
        controls.update({name: 'pointer coordinates required' for name in adapter.pointer_actions})
        content = [{'type': 'input_text', 'text': json.dumps({
            'seconds_until_next_decision': step_frames/30,
            'action_controls': controls,
            'recent_actions': [{k: d[k] for k in ('frame', 'action', 'reason')} for d in recent[-4:]]})}]
        if current_state is not None:
            content.append({'type': 'input_text', 'text':
                'CURRENT STATE (engine values): ' + json.dumps(current_state) +
                '\n' + adapter.state_description})
            if recent and isinstance(recent[-1].get('player_state'), dict):
                before = recent[-1]['player_state']
                changed = {key: {'before': before[key], 'after': value}
                           for key, value in current_state.items()
                           if key in before and before[key] != value}
                content.append({'type': 'input_text', 'text': json.dumps({
                    'last_action': recent[-1]['action'], 'observable_state_changes': changed,
                    'feedback': ('The last action did not change the observed state. Check the screen and '
                                 'choose another relevant control if the intended interaction did not happen.'
                                 if not changed else 'Verify that the change advances the current goal.')})})
        for d in recent[-2:]:
            content += [{'type': 'input_text', 'text': f"Earlier screenshot at frame {d['frame']}"}, llm.img(d['image'])]
        content += [{'type': 'input_text', 'text': 'Current screenshot; choose the next action. '
                     'Keep reason to one or two short sentences (at most 40 words); finish the JSON object.'}, llm.img(str(image))]
        if run_objective is not None:
            content.insert(0, {'type': 'input_text', 'text': json.dumps({
                'run_objective': run_objective, 'previous_playtest': previous_playtest or []})})
        value = llm.call(model, adapter.system(current_state is not None) + (skill or '(no learned skill)') +
                         (COMPLETION_PROMPT if run_objective is not None else ''), content,
                         'godot_player_action', action_schema(adapter) if run_objective is not None else adapter.action_schema,
                         backend=backend, max_tokens=500)
        return decision(value, adapter, run_objective) if run_objective is not None else value
    decide.run_objective = run_objective
    decide.adapter_id = adapter.task_id
    return decide


def reflector(model='local', backend='local', *, adapter=None):
    adapter = resolve(adapter=adapter)
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['reason', 'skill'],
              'properties': {'reason': {'type': 'string'}, 'skill': {'type': 'string'}}}
    def reflect(skill, result, history):
        import llm
        content = [{'type': 'input_text', 'text': json.dumps({'skill': skill, 'replay': result['replay'],
                    'decisions': result['decisions'], 'previous_rounds': [
                        {'reason': h['reason'], 'accepted': h['accepted']} for h in history]})}]
        # Last frames show the approach to failure, not privileged physics state.
        for d in result['decisions'][-3:]:
            content.append(llm.img(d['image']))
        return llm.call(model, f'Improve only the player skill for {adapter.task_id}. Game rules: {adapter.instructions} '
            'Return a concise list of general visual decision rules. Do not mention exact frame indices, seeds, '
            'or hardcode an action sequence. Explain the failure pattern. Do not modify game mechanics.',
            content, 'godot_behavior_skill', schema, backend=backend, max_tokens=1500)
    return reflect


def wait_json(path, predicate, proc=None, timeout=30):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError('Godot exited before observation; inspect godot.log')
        try:
            value = json.loads(Path(path).read_text())
            if predicate(value):
                return value
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(.03)
    raise TimeoutError(f'Timed out waiting for {path}')


def check_logs(project, logs):
    for p in logs:
        if p.exists() and ('SCRIPT ERROR' in p.read_text() or 'Parse Error' in p.read_text()):
            raise RuntimeError(f'Godot script failure: {p}')


def rollout(project, output, skill, decide, *, godot, step_frames=6, max_decisions=40, scenario=None,
            normal_launch=False, player_state='pixels', adapter=None,
            max_episode_frames=600):
    adapter = resolve(project, adapter)
    scenario = adapter.default_scenario if scenario is None else scenario
    adapter.validate_scenario(scenario)
    if getattr(decide, 'adapter_id', adapter.task_id) != adapter.task_id:
        raise ValueError('Player and rollout adapters must match')
    if player_state not in ('pixels', 'engine'):
        raise ValueError('player_state must be pixels or engine')
    if type(step_frames) is not int or not 1 <= step_frames <= 30:
        raise ValueError('step_frames must be 1–30')
    if (type(max_episode_frames) is not int or max_episode_frames < 1 or
            type(max_decisions) is not int or max_decisions < 1 or
            step_frames * max_decisions > max_episode_frames):
        raise ValueError('Decision budget exceeds the episode frame limit')
    from gamecraft_bench.verifier import replay as rp
    root = Path(project).resolve()
    out = Path(output).resolve()
    if root == out or root in out.parents:
        raise ValueError('Output must be outside project')
    out.mkdir(parents=True, exist_ok=False)
    initial_digest = digest(root)
    game = prepare(root, out / 'runtime-project', adapter)
    control = out / 'control'
    control.mkdir()
    os.environ.setdefault('LIBGL_ALWAYS_SOFTWARE', '1')
    os.environ.setdefault('LP_NUM_THREADS', '1')
    env = {**os.environ, 'BEHAVIOR_CONTROL_DIR': str(control), 'GODOT_SILENCE_ROOT_WARNING': '1',
           'BEHAVIOR_FRAME_TRACE': '', 'BEHAVIOR_OBSERVE_ONLY': '0'}
    from .objective import decision
    objective = getattr(decide, 'run_objective', None)
    goal_mode = objective is not None
    if goal_mode:
        # Each attempt and its verification replay start with fresh user:// saves.
        env['XDG_DATA_HOME'] = str(out / 'user-data')
    with (out / 'import.log').open('w') as stream:
        subprocess.run([godot, '--headless', '--editor', '--path', str(game), '--import'],
                       stdout=stream, stderr=subprocess.STDOUT, env=env, check=True, timeout=300)
    check_logs(game, [out / 'import.log'])
    completion = {'outcome': 'truncated', 'reason': 'Frame budget exhausted', 'source': 'runner'}
    decisions, frames = [], 0
    held_keys = HeldKeyState() if adapter.key_actions else None
    with contextlib.ExitStack() as stack:
        xlog = stack.enter_context((out / 'xvfb.log').open('w'))
        glog = stack.enter_context((out / 'godot.log').open('w'))
        xvfb, display = rp._start_xvfb(xlog, viewport=adapter.viewport)
        stack.callback(rp._stop, xvfb)
        env['DISPLAY'] = f':{display}'
        command = [godot, '--path', str(game), '--display-driver', 'x11',
              '--rendering-driver', 'opengl3', '--audio-driver', 'Dummy', '--resolution', f'{adapter.viewport[0]}x{adapter.viewport[1]}',
              '--single-window']
        if not normal_launch:
            command += ['--', '--scenario', scenario]
        proc = subprocess.Popen(command, env=env, stdout=glog, stderr=glog)
        stack.callback(rp._stop, proc)
        observation = wait_json(control / 'observation.json', lambda v: v['id'] == -1, proc)
        # One final observation check can finish at the last permitted frame,
        # but must never execute an extra action beyond the frame budget.
        for i in range(max_decisions + int(goal_mode)):
            # The bridge captures image and telemetry together while physics is paused.
            state = (adapter.policy_observation(observation) if getattr(decide, 'program_mode', False)
                     else adapter.observation(observation)) if player_state == 'engine' else None
            kwargs = {'current_state': state} if state is not None else {}
            value = decision(decide(Path(observation['image']), skill,
                                  [{k: d[k] for k in ('frame', 'action', 'reason', 'image', 'pointer', 'player_state') if k in d}
                                   for d in decisions],
                                  step_frames, **kwargs), adapter, objective)
            if goal_mode and value['done']:
                completion = {'outcome': value['outcome'], 'reason': value['reason'],
                              'source': 'player', 'verified_by_judge': False}
                break
            if i == max_decisions:
                break
            events = adapter.events(value)
            issues = []
            if held_keys is not None:
                events, issues = held_keys.dispatch(events)
            decisions.append({**value, 'frame': frames, 'image': observation['image'],
                              'evaluation_state': adapter.measured(observation),
                              **({'player_state': state} if state is not None else {}),
                              **({'emitted_events': events} if held_keys is not None else {}),
                              **({'input_issues': issues} if issues else {})})
            write_json(control / 'command.json', {'id': i, 'frames': step_frames,
                                                  'action': value['action'], 'events': events})
            observation = wait_json(control / 'observation.json', lambda v: v['id'] == i, proc)
            frames += step_frames
            print(f"[player] step={i+1} frame={frames} action={value['action']} terminal={adapter.terminal(observation)}", flush=True)
            write_json(out / 'decisions.json', decisions)
            if not goal_mode and adapter.terminal(observation):
                completion = {'outcome': 'terminal', 'reason': 'Legacy adapter terminal', 'source': 'runner'}
                break
    check_logs(game, [out / 'godot.log'])
    write_json(out / 'decisions.json', decisions)
    frame_trace = (trace(decisions, frames, adapter, max_frames=max_episode_frames) if frames else
                   {'duration_frames': 0, 'events': []})
    write_json(out / 'trace.json', {**frame_trace, 'scenario': scenario,
                                    'stop_on_terminal': not goal_mode,
                                    'normal_launch': bool(normal_launch)})
    # Compare against a separate engine execution on the same physics clock.
    # OS-keyboard replay remains available in the benchmark for compatibility
    # evaluation; its wall-clock scheduling is not a deterministic training gate.
    from .frame_replay import replay as frame_replay, equivalent
    replay, video = frame_replay(game, out / 'trace.json', out / 'frame-replay', godot,
                                 adapter=adapter, max_frames=max_episode_frames)
    measured = adapter.measured(observation)
    consistent = equivalent(measured, replay, adapter)
    if digest(root) != initial_digest:
        raise RuntimeError('Source project changed during rollout; result is not comparable')
    result = {'task_id': adapter.task_id, 'player_state_mode': player_state, 'decisions': decisions, 'stepped': measured,
              'replay': replay, 'replay_verified': consistent, 'trace': str(out / 'trace.json'),
              'video': video, 'replay_protocol': 'physics_frames_v1', 'decision_frames': step_frames,
              'project_sha256': initial_digest, 'scenario': scenario, 'normal_launch': bool(normal_launch),
              'seed': adapter.seed(root, scenario), 'run_objective': objective,
              'completion': {**completion, 'frame': frames},
              'final_player_state': adapter.policy_observation(observation) if player_state == 'engine' else {},
              'final_image': observation['image'], 'frames': frames}
    if getattr(decide, 'program_mode', False):
        result['player_policy'] = decide.policy_path
    write_json(out / 'result.json', result)
    return result
