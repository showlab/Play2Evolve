"""Independent engine-frame replay, with a movie of the scored execution.

This is an internal training verifier, not the benchmark's OS-keyboard replay.
"""
import contextlib
import json
import os
import subprocess
from pathlib import Path


def validate_trace(trace, adapter=None, *, max_frames=600):
    from .adapters import resolve
    adapter = resolve(adapter=adapter)
    if type(trace.get('stop_on_terminal', True)) is not bool:
        raise ValueError('stop_on_terminal must be boolean')
    minimum = 0 if trace.get('stop_on_terminal') is False else 1
    if (type(max_frames) is not int or max_frames < 1 or
            type(trace.get('duration_frames')) is not int or
            not minimum <= trace['duration_frames'] <= max_frames):
        raise ValueError('Invalid replay duration')
    previous = -1
    held_keys = set()
    held_mouse = False
    for event in trace.get('events', []):
        frame = event.get('frame')
        if type(frame) is not int or not previous < frame < trace['duration_frames']:
            raise ValueError('Frame replay requires ordered, unique action frames')
        kind = event.get('type')
        if kind == 'key_press':
            if event.get('keycode') not in {k for k in adapter.actions.values() if k is not None}:
                raise ValueError('Replay key is outside the adapter allowlist')
        elif kind in ('key_down', 'key_up'):
            if (kind, event.get('keycode')) not in set(adapter.key_actions.values()):
                raise ValueError('Replay held-key action is outside the adapter allowlist')
            key = event['keycode']
            if (kind == 'key_down' and key in held_keys) or (kind == 'key_up' and key not in held_keys):
                raise ValueError('Replay held-key press/release is unbalanced')
            if kind == 'key_down':
                held_keys.add(key)
            else:
                held_keys.remove(key)
        elif kind in ('mouse_click', 'mouse_drag', 'mouse_move', 'mouse_down', 'mouse_up'):
            action = kind.removeprefix('mouse_')
            if action not in adapter.pointer_actions or event.get('button') != 'left':
                raise ValueError('Replay pointer action is outside the adapter allowlist')
            keys = ('x', 'y') if kind == 'mouse_click' else ('x', 'y', 'to_x', 'to_y')
            for key in keys:
                limit = adapter.viewport[0] if key.endswith('x') else adapter.viewport[1]
                if type(event.get(key)) is not int or not 0 <= event[key] < limit:
                    raise ValueError('Replay pointer coordinates are outside the viewport')
            if kind in ('mouse_down', 'mouse_up'):
                if (kind == 'mouse_down' and held_mouse) or (kind == 'mouse_up' and not held_mouse):
                    raise ValueError('Replay pointer press/release is unbalanced')
                held_mouse = kind == 'mouse_down'
            elif kind in ('mouse_click', 'mouse_drag') and held_mouse:
                raise ValueError('Replay cannot click or drag while a pointer is held')
        else:
            raise ValueError('Unsupported replay event type')
        previous = frame


def equivalent(stepped, replay, adapter=None):
    from .adapters import resolve
    return resolve(adapter=adapter).equivalent(stepped, replay)


def replay(project, trace_path, output, godot, *, adapter=None, max_frames=600):
    from .adapters import resolve
    adapter = resolve(project, adapter)
    from gamecraft_bench.verifier import replay as rp
    from .runtime import check_logs, wait_json
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    trace_path = Path(trace_path).resolve()
    data = json.loads(trace_path.read_text())
    validate_trace(data, adapter, max_frames=max_frames)
    adapter.validate_scenario(data.get('scenario', adapter.default_scenario))
    env = {**os.environ, 'BEHAVIOR_CONTROL_DIR': str(output),
           'BEHAVIOR_FRAME_TRACE': str(trace_path), 'BEHAVIOR_OBSERVE_ONLY': '0',
           'LIBGL_ALWAYS_SOFTWARE': '1', 'LP_NUM_THREADS': '1'}
    if data.get('stop_on_terminal') is False:
        env['XDG_DATA_HOME'] = str(output / 'user-data')
    movie, video = output / 'replay.avi', output / 'replay.mp4'
    with contextlib.ExitStack() as stack:
        xlog = stack.enter_context((output / 'xvfb.log').open('w'))
        log = stack.enter_context((output / 'godot.log').open('w'))
        xvfb, display = rp._start_xvfb(xlog, viewport=adapter.viewport)
        stack.callback(rp._stop, xvfb)
        env['DISPLAY'] = f':{display}'
        command = [godot, '--path', str(project), '--display-driver', 'x11',
            '--rendering-driver', 'opengl3', '--audio-driver', 'Dummy', '--resolution', f'{adapter.viewport[0]}x{adapter.viewport[1]}',
            # Gameplay logic is advanced by the bridge at 60 Hz; render cadence
            # only affects the exported movie, never the simulated state.
            '--single-window', '--write-movie', str(movie), '--fixed-fps', '60',
        ]
        if not data.get('normal_launch', False):
            command += ['--', '--scenario', data.get('scenario', adapter.default_scenario)]
        subprocess.run(command, env=env,
            stdout=log, stderr=log, check=True,
            timeout=max(300, data['duration_frames'] / 30 * 20))
    check_logs(project, [output / 'godot.log'])
    result = wait_json(output / 'replay-status.json', lambda v: True, timeout=2)
    with (output / 'ffmpeg.log').open('w') as log:
        subprocess.run(['ffmpeg', '-y', '-i', str(movie), '-c:v', 'libx264', '-preset', 'fast',
                        '-pix_fmt', 'yuv420p', '-an', str(video)], stdout=log,
                        stderr=log, check=True,
                        timeout=max(120, data['duration_frames'] / 30 * 8))
    if not video.is_file() or not video.stat().st_size:
        raise RuntimeError('Frame replay video is empty')
    movie.unlink()  # Only the temporary movie created by this invocation.
    return result, str(video)
