"""Record learned-Player demos on a frozen final game and package a new submission.

This never edits the RSI run or its original demo_outputs. The official replay
collection is required before the new submission is marked ready for judging.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from game_initialization.package import load_adapter
from gamecraft_behavior.core import digest, write_json
from gamecraft_behavior.runtime import player, rollout
from gamecraft_behavior.runtime import check_logs

from .packaging import validate_trace


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_cases(run: Path, names: list[str]) -> list[dict]:
    cases = json.loads((run / "initial/scenarios.json").read_text())
    if not names or len(names) != len(set(names)):
        raise ValueError("Declare one or more distinct scenario IDs before recording")
    by_id = {case["id"]: case for case in cases}
    missing = [name for name in names if name not in by_id]
    if missing:
        raise ValueError("Unknown declared scenarios: " + ", ".join(missing))
    return [by_id[name] for name in names]


def public_trace(record: dict) -> dict:
    """Keep only benchmark fields; a normal launch must not inject --scenario."""
    trace = json.loads(Path(record["trace"]).read_text())
    events = []
    for event in trace["events"]:
        if event.get("type") == "mouse_drag":
            # The training bridge accepts one drag event; official OS replay
            # expects the portable down/move/up input sequence instead.
            frame, button = event["frame"], event.get("button", "left")
            events.extend((
                {"frame": frame, "type": "mouse_down", "button": button,
                 "x": event["x"], "y": event["y"]},
                {"frame": frame, "type": "mouse_move",
                 "x": event["to_x"], "y": event["to_y"]},
                {"frame": frame, "type": "mouse_up", "button": button,
                 "x": event["to_x"], "y": event["to_y"]},
            ))
        else:
            events.append(event)
    result = {"duration_frames": trace["duration_frames"], "events": events}
    if not record.get("normal_launch", False):
        scenario = record.get("scenario")
        if not isinstance(scenario, str) or not scenario:
            raise ValueError("Scenario-mode episode has no scenario")
        result["scenario"] = scenario
    return result


def existing_demo_names(project: Path, names: list[str]) -> list[Path]:
    if len(names) != len(set(names)):
        raise ValueError("Duplicate retained demo")
    paths = []
    for name in names:
        if Path(name).name != name or not name.endswith(".json"):
            raise ValueError("--keep-demo must be a plain JSON filename")
        path = project / "demo_outputs" / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing original demo: {name}")
        validate_trace(path)
        paths.append(path)
    return paths


def key_outcome_matches(expected: dict, observed: dict) -> bool:
    """Check discrete game outcome, not pixel/frame-perfect OS replay timing."""
    common = [key for key in ("state", "score", "terminal") if key in expected]
    if not common or any(key not in observed or observed[key] != expected[key] for key in common):
        return False
    if expected.get("state") == "dead" and "survival_seconds" in expected:
        return ("survival_seconds" in observed and
                abs(observed["survival_seconds"] - expected["survival_seconds"]) <= .25)
    return True


def check_os_input(record: dict, output: Path, godot: Path) -> dict:
    """Replay the exported inputs on the private observed runtime project.

    This checks key outcome parity before clean benchmark collection. The latter
    still runs separately; the bridge is never copied into the submission.
    """
    from gamecraft_bench.verifier.replay import replay_trace
    root = output / "os-input-check"
    root.mkdir()
    control = root / "control"
    control.mkdir()
    trace = root / "exported.json"
    write_json(trace, public_trace(record))
    env_keys = ("BEHAVIOR_CONTROL_DIR", "BEHAVIOR_OBSERVE_ONLY", "BEHAVIOR_FRAME_TRACE")
    saved = {key: os.environ.get(key) for key in env_keys}
    try:
        os.environ["BEHAVIOR_CONTROL_DIR"] = str(control)
        os.environ["BEHAVIOR_OBSERVE_ONLY"] = "1"
        os.environ["BEHAVIOR_FRAME_TRACE"] = ""
        replay_trace(project_dir=Path(record["trace"]).parent / "runtime-project",
                     trace_path=trace, output_mp4=root / "replay.mp4",
                     godot_bin=str(godot), log_dir=root / "logs")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    check_logs(Path(record["trace"]).parent / "runtime-project", [root / "logs/godot.log"])
    status_path = control / "replay-status.json"
    if not status_path.is_file():
        raise RuntimeError("OS-input replay produced no game status")
    observed = json.loads(status_path.read_text())
    matched = key_outcome_matches(record["replay"], observed)
    write_json(root / "comparison.json", {"expected": record["replay"],
                                            "observed": observed, "key_outcome_matched": matched})
    if not matched:
        raise RuntimeError("Exported OS inputs changed the key game outcome")
    return {"comparison": str(root / "comparison.json"), "video": str(root / "replay.mp4")}


def package(project: Path, output: Path, cases: list[dict], records: list[dict],
            retained: list[Path], *, project_hash: dict, skill_hash: str,
            policy_hash: str | None) -> Path:
    """Build a clean, independently scoreable project from verified episodes."""
    if len(cases) != len(records) or not 1 <= len(records) + len(retained) <= 10:
        raise ValueError("Expected every declared episode and at most ten total demos")
    if digest(project) != project_hash:
        raise ValueError("Final game changed during recording")
    target = output / "project"
    if target.exists():
        raise ValueError("Submission project already exists")
    if any(path.is_symlink() for path in project.rglob("*")):
        raise ValueError("Project symlinks are not supported")
    if "BehaviorBridge=" in (project / "project.godot").read_text() or (project / "behavior_bridge.gd").exists():
        raise ValueError("Development bridge cannot enter a benchmark submission")
    shutil.copytree(project, target, ignore=shutil.ignore_patterns(
        ".godot", ".git", "__pycache__", "demo_outputs"))
    demos = target / "demo_outputs"
    demos.mkdir()
    entries = []
    for index, path in enumerate(retained):
        name = f"{index:02d}_original_{path.stem}.json"
        dest = demos / name
        shutil.copyfile(path, dest)
        entries.append({"name": name, "kind": "original", "source": str(path),
                        "sha256": file_hash(dest)})
    for index, (case, record) in enumerate(zip(cases, records), len(retained)):
        source_verified = (record.get("capture_mode") == "native" or record.get("replay_verified"))
        if (not source_verified or not record.get("os_input_check")
                or record.get("project_sha256") != project_hash
                or record.get("scenario") != case["id"]
                or bool(record.get("normal_launch")) != (case.get("launch", "scenario") == "default")):
            raise ValueError(f"Episode provenance/replay mismatch: {case['id']}")
        source_trace = Path(record["trace"])
        source_video = Path(record["video"])
        comparison = Path(record["os_input_check"]["comparison"])
        if (not source_trace.is_file() or not source_video.is_file() or not source_video.stat().st_size
                or not comparison.is_file()):
            raise ValueError(f"Episode evidence is incomplete: {case['id']}")
        if json.loads(comparison.read_text()).get("key_outcome_matched") is not True:
            raise ValueError(f"OS-input outcome was not verified: {case['id']}")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", case["id"]):
            raise ValueError("Scenario ID is not safe for a demo filename")
        name = f"{index:02d}_agent_{case['id']}.json"
        dest = demos / name
        write_json(dest, public_trace(record))
        validate_trace(dest)
        entries.append({"name": name, "kind": "agent", "scenario": case["id"],
                        "source": record["trace"], "source_trace_sha256": file_hash(source_trace),
                        "source_result": str(source_trace.with_name("result.json")),
                        "source_video": record["video"], "source_video_sha256": file_hash(source_video),
                        "sha256": file_hash(dest),
                        "outcome": record["replay"], "replay_protocol": record["replay_protocol"],
                        "os_input_check": {**record["os_input_check"],
                                           "comparison_sha256": file_hash(comparison)}})
    if digest(project) != project_hash:
        raise ValueError("Final game changed while packaging")
    write_json(output / "manifest.json", {
        "protocol": "post-rsi-real-player-demos-v1", "project_source": str(project),
        "project_source_hashes": project_hash, "project_frozen_hashes": digest(target),
        "skill_sha256": skill_hash, "policy_sha256": policy_hash,
        "selection": "all predeclared episodes; no outcome-based selection or resampling",
        "demos": entries,
        "verification": {"source_episode": "native_recording_or_training_frame_replay_passed",
                         "os_input_key_outcome": "passed",
                         "official_build_and_replay": "pending",
                         "full_state_equivalence_to_clean_official_replay": "not_verified"},
    })
    return target


def _decider(project: Path, output: Path, skill: str, *, mode: str,
             backend: str, model: str, policy_path: Path | None, adapter):
    if policy_path is not None:
        if mode != "program":
            raise ValueError("--policy requires program mode")
        from gamecraft_behavior.program_policy import compile_policy
        policy = compile_policy(policy_path.read_text())
        def decide(image, text, recent, step_frames, *, current_state=None):
            if current_state is None:
                raise ValueError("Saved policy needs structured engine state")
            return policy(current_state, [{k: item[k] for k in ("frame", "action", "reason")}
                                          for item in recent])
        decide.program_mode = True
        decide.policy_path = str(policy_path)
        decide.adapter_id = adapter.task_id
        return decide
    if mode == "program":
        from gamecraft_behavior.program_policy import program_player
        return program_player(project, output / "policy.py", model=model, backend=backend, adapter=adapter)
    return player(model=model, backend=backend, adapter=adapter)


def collect_official(project: Path, output: Path, bench: Path, godot: Path) -> dict:
    """Build and record every demo using the benchmark's clean OS-input replay."""
    with (output / "import.log").open("w") as log:
        subprocess.run([str(godot), "--headless", "--editor", "--path", str(project), "--import"],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    check_logs(project, [output / "import.log"])
    with (output / "collection.log").open("w") as log:
        subprocess.run([sys.executable, str(Path(__file__).with_name("collect_replays.py")),
                        "--bench", str(bench), "--task", json.loads((project / "game_plan.json").read_text())["task_id"],
                        "--project", str(project), "--godot", str(godot),
                        "--output", str(output / "collection")],
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
    evidence = json.loads((output / "collection/collection.json").read_text())
    expected = {path.stem for path in (project / "demo_outputs").glob("*.json")}
    if (not evidence["build_ok"] or evidence["errors"]
            or set(evidence["demos_collected"]) != expected):
        raise RuntimeError("Official build/replay collection did not cover every demo")
    return evidence


def run(*, run_dir: Path, output: Path, bench: Path, godot: Path, scenario_ids: list[str],
        keep_demos: list[str], step_frames: int, max_decisions: int,
        backend: str | None = None, model: str | None = None,
        mode: str | None = None, policy_path: Path | None = None,
        capture_mode: str | None = None) -> Path:
    run_dir, output, bench, godot = (path.resolve() for path in (run_dir, output, bench, godot))
    if (output.exists() or output == run_dir or run_dir in output.parents
            or output == bench or bench in output.parents):
        raise ValueError("Output must be new and outside the source run/benchmark")
    info = json.loads((run_dir / "result.json").read_text())
    config = json.loads((run_dir / "run.json").read_text())
    if (info.get("profile") != "rsi" or config.get("profile") != "rsi"
            or info.get("state") != "completed"):
        raise ValueError("Expected a completed RSI run")
    project = Path(info["project"]).resolve()
    skill_path = Path(info["skill"]).resolve()
    if (not (project / "project.godot").is_file() or not skill_path.is_file()
            or not godot.is_file() or not (bench / "tasks" / config["task_id"] / "tests/rubric.json").is_file()):
        raise ValueError("Final game, skill, Godot or benchmark task is missing")
    if json.loads((project / "game_plan.json").read_text())["task_id"] != config["task_id"]:
        raise ValueError("RSI task and final game task differ")
    adapter = load_adapter(run_dir / "initial")
    cases = select_cases(run_dir, scenario_ids)
    retained = existing_demo_names(project, keep_demos)
    if len(cases) + len(retained) > 10:
        raise ValueError("At most ten retained and Agent demos are allowed")
    if type(step_frames) is not int or type(max_decisions) is not int or not 1 <= step_frames <= 30 or not 1 <= max_decisions or step_frames * max_decisions > 600:
        raise ValueError("Episode budget must fit in 600 frames / 20 seconds")
    settings = config.get("player") or {}
    backend, model, mode = (backend or settings.get("backend", "local"),
                            model or settings.get("model", "local"),
                            mode or settings.get("mode", "interactive"))
    if backend not in ("local", "api", "codex") or mode not in ("interactive", "program"):
        raise ValueError("Unknown Player backend or mode")
    if backend != "local" and model == "local" and policy_path is None:
        raise ValueError("An explicit model is required for nonlocal Player")
    if mode == "program" and config.get("player_state") != "engine":
        raise ValueError("Program Player requires engine-state observations")
    if policy_path is not None:
        policy_path = policy_path.resolve()
        if not policy_path.is_file():
            raise ValueError("Saved policy does not exist")
    capture_mode = capture_mode or ("native" if mode == "program" else "frame")
    if capture_mode not in ("native", "frame"):
        raise ValueError("Unknown capture mode")
    if capture_mode == "native" and mode != "program":
        raise ValueError("Native capture requires program mode")
    project_hash = digest(project)
    skill_hash = file_hash(skill_path)
    saved_policy_hash = file_hash(policy_path) if policy_path else None
    skill = skill_path.read_text().strip()
    output.mkdir(parents=True)
    write_json(output / "plan.json", {
        "run": str(run_dir), "project": str(project), "project_hashes": project_hash,
        "skill": str(skill_path), "skill_sha256": skill_hash,
        "scenarios": scenario_ids, "keep_demos": keep_demos,
        "step_frames": step_frames, "max_decisions": max_decisions,
        "player": {"backend": backend, "model": model, "mode": mode,
                   "saved_policy": str(policy_path) if policy_path else None,
                   "policy_source": ("saved" if policy_path else
                                     "authored_from_final_skill" if capture_mode == "native" else "frame_player"),
                   "capture_mode": capture_mode},
    })
    try:
        os.environ["LLM_BACKEND"] = backend
        os.environ["OPTIMIZER_BACKEND"] = backend
        os.environ["GAMECRAFT_BENCH_GODOT_BIN"] = str(godot)
        decider = (_decider(project, output, skill, mode=mode, backend=backend,
                            model=model, policy_path=policy_path, adapter=adapter)
                   if capture_mode == "frame" or policy_path is None else None)
        if capture_mode == "native" and policy_path is None:
            # Author the bounded reactive policy from the FINAL Skill on the
            # FINAL game. This one-step prelude is not submitted as a demo.
            first = cases[0]
            authored = rollout(project, output / "policy-authorship", skill, decider,
                               godot=str(godot), adapter=adapter, scenario=first["id"],
                               normal_launch=first.get("launch", "scenario") == "default",
                               step_frames=step_frames, max_decisions=1, player_state="engine")
            if not authored["replay_verified"]:
                raise RuntimeError("Final-Skill policy authorship replay failed")
            policy_path = output / "policy.py"
            if not policy_path.is_file():
                raise RuntimeError("Final-Skill program was not saved")
            saved_policy_hash = file_hash(policy_path)
        episodes = output / "episodes"
        episodes.mkdir()
        records = []
        for index, case in enumerate(cases):
            destination = episodes / f"{index:04d}"
            if capture_mode == "native":
                from .native_player import record as record_native
                record = record_native(project, destination, policy_path, adapter=adapter,
                                       godot=godot, scenario=case["id"],
                                       normal_launch=case.get("launch", "scenario") == "default",
                                       step_frames=step_frames, max_decisions=max_decisions)
            else:
                record = rollout(project, destination, skill, decider,
                                 godot=str(godot), adapter=adapter, scenario=case["id"],
                                 normal_launch=case.get("launch", "scenario") == "default",
                                 step_frames=step_frames, max_decisions=max_decisions,
                                 player_state=config.get("player_state", "engine"))
                if not record["replay_verified"]:
                    raise RuntimeError(f"Training frame replay mismatch: {case['id']}")
            record["os_input_check"] = check_os_input(record, episodes / f"{index:04d}", godot)
            records.append(record)
        if file_hash(skill_path) != skill_hash or (policy_path and file_hash(policy_path) != saved_policy_hash):
            raise ValueError("Behavior Skill or saved Player policy changed during recording")
        policy_hash = saved_policy_hash if policy_path else (
            file_hash(output / "policy.py") if (output / "policy.py").is_file() else None)
        submission = package(project, output, cases, records, retained,
                             project_hash=project_hash, skill_hash=skill_hash,
                             policy_hash=policy_hash)
        evidence = collect_official(submission, output, bench, godot)
        manifest = json.loads((output / "manifest.json").read_text())
        manifest["verification"]["official_build_and_replay"] = "passed"
        manifest["verification"]["collection"] = str(output / "collection/collection.json")
        manifest["verification"]["demos_collected"] = evidence["demos_collected"]
        write_json(output / "manifest.json", manifest)
        write_json(output / "status.json", {"state": "ready_for_judging", "project": str(submission),
                                            "collection": str(output / "collection")})
        return submission
    except Exception as exc:
        comparisons = sorted((output / "episodes").glob("*/os-input-check/comparison.json")) if (output / "episodes").is_dir() else []
        write_json(output / "status.json", {"state": "failed", "reason": f"{type(exc).__name__}: {exc}",
                                             "os_input_comparisons": [str(path) for path in comparisons]})
        raise
