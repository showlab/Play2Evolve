"""Retry identical continuous input once when OS input scheduling diverges.

No new policy is sampled and no gate tolerance is relaxed. Both outcomes are
kept; persistent disagreement remains a hard error in the caller.
"""
import os
from pathlib import Path

from gamecraft_behavior.core import write_json
from gamecraft_behavior.runtime import check_logs, wait_json


def verify(result, output, godot, replay_trace=None):
    if result['replay_verified']:
        return result
    if replay_trace is None:
        from gamecraft_bench.verifier.replay import replay_trace
    output = Path(output)
    retry = output / 'replay-retry'
    retry.mkdir()
    control = retry / 'control'
    control.mkdir()
    saved = {k: os.environ.get(k) for k in ('BEHAVIOR_CONTROL_DIR', 'BEHAVIOR_OBSERVE_ONLY')}
    try:
        os.environ['BEHAVIOR_CONTROL_DIR'] = str(control)
        os.environ['BEHAVIOR_OBSERVE_ONLY'] = '1'
        replay_trace(project_dir=output / 'runtime-project', trace_path=Path(result['trace']),
                     output_mp4=retry / 'replay.mp4', godot_bin=godot, log_dir=retry / 'logs')
    finally:
        for k, value in saved.items():
            if value is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = value
    check_logs(output / 'runtime-project', [retry / 'logs/godot.log'])
    replay = wait_json(control / 'replay-status.json', lambda v: True, timeout=2)
    stepped = result['stepped']
    consistent = (replay['score'] == stepped['score'] and replay['state'] == stepped['state'] and
                  abs(replay['survival_seconds'] - stepped['survival_seconds']) <= .25)
    result = {**result, 'replay_attempts': [
        {'replay': result['replay'], 'video': result['video'], 'verified': False},
        {'replay': replay, 'video': str(retry / 'replay.mp4'), 'verified': consistent}],
        'replay': replay, 'video': str(retry / 'replay.mp4'), 'replay_verified': consistent}
    write_json(output / 'result.json', result)
    return result
