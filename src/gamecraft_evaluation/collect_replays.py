"""Run only the official GameCraft-Bench BUILD/replay stages; judge elsewhere."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

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

from gamecraft_bench import config as bench_config
from gamecraft_bench.verifier.score import score_project
from gamecraft_bench.verifier.judges.base import JudgeResponse, MultimodalJudge

from gamecraft_evaluation.direct import dump, tree_hash
from gamecraft_evaluation.packaging import validate_trace
from gamecraft_evaluation.scoring import adapted_rubric


class CollectOnlyJudge(MultimodalJudge):
    name = "replay-collection-only"
    default_model = "none"

    def score(self, request):
        ids = [r.id for r in request.requirements]
        # These placeholder zeros are discarded; this stage only creates the
        # official build check, replay recordings and sampled-frame evidence.
        return JudgeResponse({rid: 0.0 for rid in ids}, {rid: "Not judged in collection stage." for rid in ids})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    bench, project, godot, output = (p.resolve() for p in (args.bench, args.project, args.godot, args.output))
    if output.exists():
        raise SystemExit(f"output already exists: {output}")
    if not (project / "project.godot").is_file() or not godot.is_file():
        raise SystemExit("project or Godot binary is missing")
    rubric_path = bench / "tasks" / args.task / "tests/rubric.json"
    rubric = json.loads(rubric_path.read_text())
    traces = sorted((project / "demo_outputs").glob("*.json"))
    if not traces or len(traces) > rubric.get("max_demos", 10):
        raise SystemExit("invalid number of official demo traces")
    for trace in traces:
        validate_trace(trace)

    output.mkdir(parents=True)
    frozen_project = output / "project"
    shutil.copytree(project, frozen_project, ignore=shutil.ignore_patterns(".godot", ".git", "__pycache__"))
    imported = subprocess.run(
        [str(godot), "--headless", "--editor", "--import", "--path", str(frozen_project)],
        capture_output=True, text=True, timeout=120,
    )
    import_log = (imported.stdout or "") + (imported.stderr or "")
    (output / "import.log").write_text(import_log)
    if imported.returncode or "SCRIPT ERROR" in import_log:
        raise RuntimeError(f"Godot asset import failed; see {output / 'import.log'}")
    (output / "userdata").mkdir()
    os.environ["XDG_DATA_HOME"] = str(output / "userdata")
    os.environ["GAMECRAFT_BENCH_GODOT_BIN"] = str(godot)
    bench_config.GODOT_BIN = str(godot)
    runtime_rubric = adapted_rubric(rubric_path, frozen_project, str(godot))
    dump(output / "runtime-rubric.json", runtime_rubric)
    dump(output / "collection.json", {
        "task": args.task, "project": str(frozen_project), "project_source": str(project),
        "source_hashes": tree_hash(frozen_project),
        "godot": str(godot), "build_ok": None,
        "interpretation": "Replay collection only. Placeholder judge scores in pass-01/breakdown.json must be discarded.",
        "demos_expected": [p.stem for p in traces],
    })
    result = score_project(
        project_dir=frozen_project, rubric_path=output / "runtime-rubric.json",
        output_dir=output / "pass-01", judge=CollectOnlyJudge(),
        fps=30, viewport=(1280, 720), record_size=(854, 480), frame_interval_seconds=0.5,
    )
    metadata = json.loads((output / "collection.json").read_text())
    metadata["build_ok"] = result.build_ok
    metadata["errors"] = result.errors
    metadata["demos_collected"] = [d.demo_id for d in result.demos]
    dump(output / "collection.json", metadata)
    if not result.build_ok or result.errors or {d.demo_id for d in result.demos} != set(metadata["demos_expected"]):
        raise RuntimeError(f"BUILD/replay collection incomplete; inspect {output / 'pass-01'}")
    print(f"BUILD={result.build_ok}; collected {len(result.demos)} demos under {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
