"""Record a saved reactive Player policy against a continuously running Godot game.

Unlike frame-stepped training, this uses the same X11 key injection and wall
clock as the benchmark replayer. It is intentionally limited to an already
authored, bounded program policy: an LLM call cannot fit inside the live game
clock without changing gameplay timing.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

from gamecraft_behavior.core import digest, prepare, write_json
from gamecraft_behavior.program_policy import compile_policy
from gamecraft_behavior.runtime import check_logs, wait_json


def record(project: Path, output: Path, policy_path: Path, *, adapter, godot: Path,
           scenario: str, normal_launch: bool, step_frames: int,
           max_decisions: int) -> dict:
    from gamecraft_bench.verifier import replay as rp

    project, output, policy_path = project.resolve(), output.resolve(), policy_path.resolve()
    policy = compile_policy(policy_path.read_text())
    source_hash = digest(project)
    output.mkdir(parents=True, exist_ok=False)
    runtime = prepare(project, output / "runtime-project", adapter)
    control = output / "control"
    control.mkdir()
    env = {**os.environ, "BEHAVIOR_CONTROL_DIR": str(control),
           "BEHAVIOR_OBSERVE_ONLY": "1", "BEHAVIOR_FRAME_TRACE": "",
           "BEHAVIOR_NATIVE_CAPTURE": "1",
           "LIBGL_ALWAYS_SOFTWARE": "1", "LP_NUM_THREADS": "1",
           "GODOT_SILENCE_ROOT_WARNING": "1"}
    with (output / "import.log").open("w") as log:
        subprocess.run([str(godot), "--headless", "--editor", "--path", str(runtime), "--import"],
                       stdout=log, stderr=subprocess.STDOUT, env=env, check=True, timeout=180)
    check_logs(runtime, [output / "import.log"])
    width, height = adapter.viewport
    duration = step_frames * max_decisions / 30
    decisions, events = [], []
    with contextlib.ExitStack() as stack:
        xvfb_log = stack.enter_context((output / "xvfb.log").open("w"))
        xvfb, display = rp._start_xvfb(xvfb_log, viewport=adapter.viewport)
        stack.callback(rp._stop, xvfb)
        env["DISPLAY"] = f":{display}"
        godot_log = stack.enter_context((output / "godot.log").open("w"))
        command = [str(godot), "--path", str(runtime), "--display-driver", "x11",
                   "--rendering-driver", "opengl3", "--audio-driver", "Dummy",
                   "--resolution", f"{width}x{height}", "--single-window"]
        if not normal_launch:
            command += ["--", "--scenario", scenario]
        game = subprocess.Popen(command, env=env, stdout=godot_log, stderr=godot_log)
        stack.callback(rp._stop, game)
        rendered = wait_json(control / "render-ready.json", lambda value: value.get("ready") is True,
                             proc=game, timeout=60)
        initial_frame = Path(rendered["image"])
        if not initial_frame.is_file() or not initial_frame.stat().st_size:
            raise RuntimeError("Godot reported a rendered frame without an image")
        window_id = rp._find_godot_window(env, pid=game.pid,
                                          timeout=rp._godot_window_timeout_seconds())
        rp._xdotool(env, "windowfocus", "--sync", window_id)
        ffmpeg_log = stack.enter_context((output / "ffmpeg.log").open("w"))
        video = output / "native-play.mp4"
        recorder = subprocess.Popen([
            "ffmpeg", "-y", "-f", "x11grab", "-framerate", "30",
            "-video_size", f"{width}x{height}", "-i", env["DISPLAY"],
            "-vf", "scale=854:480:flags=lanczos", "-t", f"{duration + 2:.3f}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video),
        ], env=env, stdin=subprocess.PIPE, stdout=ffmpeg_log, stderr=ffmpeg_log)
        stack.callback(rp._stop, recorder)
        rp._wait_for_ffmpeg_capture(recorder, output / "ffmpeg.log", timeout=15.0)
        # The game remains paused on its first rendered frame while the recorder
        # starts. Include a short visible, input-free opening before gameplay.
        time.sleep(0.5)
        write_json(control / "capture-start.json", {"start": True})
        wait_json(control / "capture-running.json", lambda value: value.get("ready") is True,
                  proc=game, timeout=5)
        start = time.monotonic()
        for index in range(max_decisions):
            target = start + index * step_frames / 30
            while time.monotonic() < target:
                time.sleep(min(.005, target - time.monotonic()))
            if game.poll() is not None:
                raise RuntimeError("Game exited during native Player recording")
            observation = wait_json(control / "replay-status.json", lambda value: True,
                                    proc=game, timeout=2)
            state = adapter.policy_observation(observation)
            recent = [{key: item[key] for key in ("frame", "action", "reason")}
                      for item in decisions]
            decision = adapter.validate_action(policy(state, recent))
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                raise RuntimeError("Policy decision missed the declared demo window")
            input_frame = math.floor(elapsed * 30)
            for event in adapter.events(decision):
                rp._post_event(event, env, window_id=window_id)
                events.append({"frame": input_frame, **event})
            decisions.append({"frame": input_frame, **decision,
                              "observation": observation})
            write_json(output / "decisions.json", decisions)
        end = start + duration
        while time.monotonic() < end:
            time.sleep(min(.005, end - time.monotonic()))
        observed = wait_json(control / "replay-status.json", lambda value: True,
                             proc=game, timeout=2)
        with contextlib.suppress(Exception):
            assert recorder.stdin is not None
            recorder.stdin.write(b"q\n")
            recorder.stdin.flush()
        recorder.wait(timeout=15)
        if not video.is_file() or not video.stat().st_size:
            raise RuntimeError("Native Player video is empty")
    check_logs(runtime, [output / "godot.log"])
    if digest(project) != source_hash:
        raise RuntimeError("Final game changed during native Player recording")
    trace = {"duration_frames": step_frames * max_decisions, "events": events}
    if not normal_launch:
        trace["scenario"] = scenario
    write_json(output / "trace.json", trace)
    result = {"capture_mode": "native", "task_id": adapter.task_id,
              "project_sha256": source_hash, "scenario": scenario,
              "normal_launch": normal_launch, "decision_frames": step_frames,
              "trace": str(output / "trace.json"), "video": str(video),
              "replay": observed, "replay_verified": False,
              "replay_protocol": "native_os_input_v1",
              "initial_frame": str(initial_frame),
              "policy_path": str(policy_path), "decisions": decisions}
    write_json(output / "result.json", result)
    return result
