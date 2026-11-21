"""Thin public launcher; optimization decisions remain in the copied pipeline."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tomllib

from .runtime import benchmark_root, configure_benchmark, godot_binary

COMMANDS = {
    "initialization": ("game_initialization.agentic", []),
    "refinement": ("gamecraft_training.package", ["--profile", "initialization"]),
    "evolution": ("gamecraft_training.package", ["--profile", "rsi"]),
    "showcase": ("gamecraft_evaluation", ["rsi-showcase"]),
    "evaluate": ("gamecraft_evaluation", []),
    "package": ("game_initialization", []),
    "asg": ("play2evolve.asg", []),
}

# Preserve the preparation copy's commands without changing their meaning.
ALIASES = {"bootstrap": "initialization", "initialize": "refinement", "rsi": "evolution"}


def load_config(path: Path) -> None:
    """Config fills unset environment values; explicit environment wins."""
    config = tomllib.loads(path.read_text())
    if set(config) - {"environment"}:
        raise ValueError("Only the [environment] table is supported")
    for key, value in config.get("environment", {}).items():
        if not isinstance(value, str):
            raise ValueError(f"Config environment value {key} must be a string")
        if key.endswith(("API_KEY", "TOKEN", "PASSWORD")) or key in {"HOME", "CODEX_HOME"}:
            raise ValueError(f"Set {key} in your shell, not in a configuration file")
        os.environ.setdefault(key, value)


def doctor() -> int:
    tools = {name: shutil.which(binary) for name, binary in {
        "godot": godot_binary(), "ffmpeg": "ffmpeg", "ffprobe": "ffprobe",
        "Xvfb": "Xvfb", "xdotool": "xdotool", "codex": os.getenv("CODEX_BIN", "codex"),
    }.items()}
    modules = {name: importlib.util.find_spec(name) is not None
               for name in ("openai", "httpx", "numpy", "PIL", "cv2", "imageio")}
    bench = benchmark_root(required=False)
    valid_bench = (bench / "gamecraft_bench/verifier/replay.py").is_file()
    print(json.dumps({"python": sys.version.split()[0], "source": str(Path(__file__).parent),
                      "tools": tools, "modules": modules,
                      "benchmark": str(bench), "benchmark_available": valid_bench,
                      "note": "Local checks only; no model requests or account changes."}, indent=2))
    return 0 if all(tools.values()) and all(modules.values()) and valid_bench else 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="play2evolve",
        description="Play2Evolve — Game Initialization → Game Refinement → Game Evolution",
        epilog="Compatibility aliases: bootstrap → initialization; initialize → refinement; rsi → evolution.")
    parser.add_argument("--config", type=Path, help="Optional TOML environment configuration")
    parser.add_argument("command", choices=[*COMMANDS, "doctor", *ALIASES])
    parser.add_argument("args", nargs=argparse.REMAINDER, help="Arguments forwarded to the existing stage")
    args = parser.parse_args(argv)
    if args.config:
        load_config(args.config)
    if args.command == "doctor":
        raise SystemExit(doctor())
    # The public entry defaults to account-based Codex; explicit backend choices win.
    os.environ.setdefault("LLM_BACKEND", "codex")
    os.environ.setdefault("OPTIMIZER_BACKEND", os.environ["LLM_BACKEND"])
    if args.command != "asg" and os.environ.get("GAMECRAFT_BENCH_ROOT"):
        configure_benchmark()
    command = ALIASES.get(args.command, args.command)
    if args.command in ALIASES:
        print(f"Compatibility alias: use 'play2evolve {command}' for this stage.", file=sys.stderr)
    module, prefix = COMMANDS[command]
    if command in ("refinement", "evolution") and any(
            v == "--profile" or v.startswith("--profile=") for v in args.args):
        parser.error("The refinement/evolution command selects its profile; omit --profile")
    sys.argv = ["play2evolve " + command, *prefix, *args.args]
    runpy.run_module(module, run_name="__main__")
