"""Score one frozen GameCraft-Bench Godot project via the logged-in Codex CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

CODE_WORLD = Path(__file__).resolve().parents[1]
WORKSPACE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE_WORLD))
from play2evolve.runtime import configure_benchmark
# Support direct script invocation and the explicit --bench argument.
_bench_arg = next((v.split('=', 1)[1] for v in sys.argv if v.startswith('--bench=')), None)
if _bench_arg is None and '--bench' in sys.argv:
    _bench_index = sys.argv.index('--bench') + 1
    if _bench_index < len(sys.argv):
        _bench_arg = sys.argv[_bench_index]
configure_benchmark(_bench_arg)

from gamecraft_bench.verifier.score import score_project
from gamecraft_bench import config as bench_config
from gamecraft_bench.verifier.judges.base import JudgeRequest, RequirementSpec
from gamecraft_bench.verifier.score import _aggregate, _safe_eval_formula

from gamecraft_evaluation.codex_judge import CodexJudge
from gamecraft_evaluation.direct import categories, dump, sha, tree_hash
from gamecraft_evaluation.packaging import validate_trace
from gamecraft_evaluation.scoring import adapted_rubric
from codex_provider import from_environment as codex_provider_from_environment
from gamecraft_evaluation.showcase_coverage import require_ready_for_scoring


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-evidence", type=Path,
                        help="Existing official replay collection; skip local Xvfb replay")
    parser.add_argument("--model", default="gpt-6-sol")
    parser.add_argument("--passes", type=int, default=3)
    args = parser.parse_args()
    provider = codex_provider_from_environment()

    bench, source, godot, out = (p.resolve() for p in (args.bench, args.project, args.godot, args.output))
    require_ready_for_scoring(source, args.replay_evidence)
    if not shutil.which("codex"):
        raise SystemExit("Codex CLI is not installed on this host")
    if not 1 <= args.passes <= 5:
        raise SystemExit("passes must be between 1 and 5")
    rubric_path = bench / "tasks" / args.task / "tests/rubric.json"
    if not rubric_path.is_file() or not (bench / "gamecraft_bench").is_dir():
        raise SystemExit("benchmark checkout or task rubric not found")
    if not source.is_dir() or not (source / "project.godot").is_file():
        raise SystemExit("project.godot is missing")
    if not godot.is_file() or not os.access(godot, os.X_OK):
        raise SystemExit("Godot executable is missing")
    traces = sorted((source / "demo_outputs").glob("*.json"))
    rubric = json.loads(rubric_path.read_text())
    if not traces or len(traces) > rubric.get("max_demos", 10):
        raise SystemExit("project needs a valid set of demo traces within the benchmark cap")
    for trace in traces:
        validate_trace(trace)
    if out.exists() or source == out or source in out.parents or out in source.parents:
        raise SystemExit("output must be a new directory separate from the source project")

    out.mkdir(parents=True)
    project = out / "project"
    shutil.copytree(source, project, ignore=shutil.ignore_patterns(".godot", ".git", "__pycache__"))
    import_result = subprocess.run(
        [str(godot), "--headless", "--editor", "--import", "--path", str(project)],
        capture_output=True, text=True, timeout=120,
    )
    (out / "import.log").write_text((import_result.stdout or "") + (import_result.stderr or ""))
    if import_result.returncode:
        raise RuntimeError(f"Godot asset import failed; see {out / 'import.log'}")
    (out / "userdata").mkdir()
    os.environ["XDG_DATA_HOME"] = str(out / "userdata")
    os.environ["GAMECRAFT_BENCH_GODOT_BIN"] = str(godot)
    bench_config.GODOT_BIN = str(godot)
    frozen_hashes = tree_hash(project)
    runtime_rubric = adapted_rubric(rubric_path, project, str(godot))
    dump(out / "official-rubric.json", rubric)
    dump(out / "runtime-rubric.json", runtime_rubric)
    dump(out / "configuration.json", {
        "mode": "score-project", "task": args.task,
        "judge": "codex-cli", "judge_model": args.model,
        "judge_provider": provider['codex_provider'],
        "judge_auth": ("existing Codex CLI login" if provider['codex_provider'] == 'openai'
                       else "explicit custom provider credential"),
        "passes": args.passes, "bench": str(bench), "project_source": str(source),
        "project_frozen": str(project), "godot": str(godot),
        "rubric_sha256": sha(rubric_path), "input_hashes": frozen_hashes,
        "demo_names": [p.stem for p in traces],
        "repeat_scope": "independent replay and Codex CLI judgement using the same frozen project",
    })

    summaries = []
    if args.replay_evidence:
        evidence = args.replay_evidence.resolve()
        metadata_path = evidence / "collection.json"
        breakdown_path = evidence / "pass-01" / "breakdown.json"
        if not metadata_path.is_file() or not breakdown_path.is_file():
            raise SystemExit("replay evidence is incomplete")
        metadata = json.loads(metadata_path.read_text())
        if not metadata.get("build_ok") or metadata.get("source_hashes") != frozen_hashes:
            raise SystemExit("replay evidence build or frozen-project hash does not match")
        collected = json.loads(breakdown_path.read_text())
        expected_ids = [p.stem for p in traces]
        if {d["demo_id"] for d in collected.get("demos", [])} != set(expected_ids):
            raise SystemExit("replay evidence does not include every frozen demo")
        req_specs = [RequirementSpec(id=r["id"], description=r["description"])
                     for r in rubric["requirements"]]
        judge = CodexJudge(model=args.model)
        for index in range(1, args.passes + 1):
            pass_dir = out / f"pass-{index:02d}"
            pass_dir.mkdir()
            started = time.monotonic()
            per_requirement = {r["id"]: {} for r in rubric["requirements"]}
            judge_logs = []
            for demo in sorted(collected["demos"], key=lambda d: d["demo_id"]):
                request = JudgeRequest(
                    demo_id=demo["demo_id"], video_path=Path(demo["mp4"]),
                    frame_paths=[Path(f) for f in demo["frames"]], requirements=req_specs,
                )
                response = judge.score(request)
                judge_logs.append({"demo_id": demo["demo_id"], "scores": response.scores,
                                   "rationales": response.rationales, "raw": response.raw})
                for rid, value in response.scores.items():
                    per_requirement[rid][demo["demo_id"]] = value
            values = {r["id"]: _aggregate(r.get("agg", "max"), per_requirement[r["id"]])
                      for r in rubric["requirements"]}
            build_id = rubric["build_check"].get("id", "BUILD")
            overall = _safe_eval_formula(rubric["score_formula"], {build_id: 1.0, **values})
            cats = categories(rubric, values)
            dump(pass_dir / "judge_log.json", judge_logs)
            dump(pass_dir / "breakdown.json", {"build_ok": True, "reward": overall,
                 "formula": rubric["score_formula"], "judge": {"name": judge.name, "model": args.model},
                 "requirements": [{"id": r["id"], "agg": r.get("agg", "max"),
                                   "aggregated": values[r["id"]],
                                   "per_demo": per_requirement[r["id"]]} for r in rubric["requirements"]],
                 "categories": cats, "demos": [d["demo_id"] for d in collected["demos"]],
                 "replay_evidence": str(evidence)})
            summary = {"pass": index, "valid": True, "build_ok": True,
                       "overall": overall, "categories": cats, "requirements": values,
                       "judge": judge.name, "judge_model": args.model,
                       "wall_seconds": time.monotonic() - started}
            summaries.append(summary)
            dump(out / "passes.json", summaries)
            print(f"PASS {index}: overall={overall * 100:.2f}/100", flush=True)
        final = {
            "mode": "score-project", "task": args.task,
            "interpretation": "Official rubric and all official demos; existing replay evidence was reused, and Codex judged each frozen recording.",
            "judge": judge.name, "judge_model": args.model,
            "passes": summaries, "overall_mean": statistics.mean(x["overall"] for x in summaries),
            "overall_std_population": statistics.pstdev(x["overall"] for x in summaries),
            "categories_mean": {key: statistics.mean(x["categories"][key] for x in summaries)
                                for key in summaries[0]["categories"]},
            "scale": "0–1; multiply by 100 for percentage points",
        }
        dump(out / "summary.json", final)
        print(f"Overall mean: {final['overall_mean'] * 100:.2f}/100")
        print(out / "summary.json")
        return 0

    for index in range(1, args.passes + 1):
        started = time.monotonic()
        pass_dir = out / f"pass-{index:02d}"
        result = score_project(
            project_dir=project,
            rubric_path=out / "runtime-rubric.json",
            output_dir=pass_dir,
            judge=CodexJudge(model=args.model),
            fps=30, viewport=(1280, 720), record_size=(854, 480),
            frame_interval_seconds=0.5,
        )
        if not result.build_ok:
            raise RuntimeError(f"GameCraft-Bench BUILD gate failed; inspect {pass_dir / 'build.log'}")
        if result.errors:
            raise RuntimeError(f"invalid scoring pass; see {pass_dir}: {result.errors}")
        if result.build_ok and {d.demo_id for d in result.demos} != {p.stem for p in traces}:
            raise RuntimeError("not all frozen demos were evaluated")
        req_values = {r.requirement_id: r.aggregated for r in result.requirements}
        summary = {
            "pass": index, "valid": True, "build_ok": result.build_ok,
            "overall": result.reward, "categories": categories(rubric, req_values),
            "requirements": req_values, "judge": result.judge_name,
            "judge_model": result.judge_model, "wall_seconds": time.monotonic() - started,
        }
        summaries.append(summary)
        dump(out / "passes.json", summaries)
        print(f"PASS {index}: BUILD={result.build_ok}, overall={result.reward * 100:.2f}/100", flush=True)
        if tree_hash(project) != frozen_hashes:
            raise RuntimeError("frozen project changed during evaluation")

    final = {
        "mode": "score-project", "task": args.task,
        "interpretation": "One-task GameCraft-Bench score using the official rubric and Codex CLI GPT-6 Sol judge; not the benchmark-wide mean.",
        "judge": "codex-cli", "judge_model": args.model,
        "passes": summaries, "overall_mean": statistics.mean(x["overall"] for x in summaries),
        "overall_std_population": statistics.pstdev(x["overall"] for x in summaries),
        "categories_mean": {key: statistics.mean(x["categories"][key] for x in summaries)
                            for key in summaries[0]["categories"]},
        "scale": "0–1; multiply by 100 for percentage points",
    }
    dump(out / "summary.json", final)
    print(f"Overall mean: {final['overall_mean'] * 100:.2f}/100")
    print(out / "summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
