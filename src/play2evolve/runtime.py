"""Resolve explicitly configured tools without research-workspace dependencies."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import sys


def benchmark_root(*, required: bool = True) -> Path:
    configured = os.environ.get("GAMECRAFT_BENCH_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        spec = importlib.util.find_spec("gamecraft_bench")
        root = (Path(spec.origin).resolve().parent.parent if spec and spec.origin
                else (Path.cwd() / "GameCraft-Bench").resolve())
    if required and not (root / "gamecraft_bench/verifier/replay.py").is_file():
        raise RuntimeError("Set GAMECRAFT_BENCH_ROOT to your GameCraft-Bench checkout "
                           "(must contain gamecraft_bench/verifier/replay.py).")
    return root


def configure_benchmark(path: str | Path | None = None) -> Path:
    root = Path(path).expanduser().resolve() if path is not None else benchmark_root()
    if not (root / "gamecraft_bench/verifier/replay.py").is_file():
        raise RuntimeError(f"Invalid GameCraft-Bench checkout: {root}")
    loaded = sys.modules.get("gamecraft_bench")
    if loaded and getattr(loaded, "__file__", None):
        actual = Path(loaded.__file__).resolve().parent.parent
        if actual != root:
            raise RuntimeError(f"GameCraft-Bench already imported from {actual}; requested {root}")
    os.environ["GAMECRAFT_BENCH_ROOT"] = str(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def godot_binary() -> str:
    return os.environ.get("GAMECRAFT_BENCH_GODOT_BIN") or shutil.which("godot") or "godot"


def asset_python() -> str:
    return os.environ.get("GAMECRAFT_ASSET_PYTHON") or sys.executable

