"""Paper-stage names for the existing HTML/GameASG implementations."""
from __future__ import annotations

import argparse
import runpy
import sys

COMMANDS = {
    "initialization": ("gameasg.initialization", ["--stage", "bootstrap"]),
    "refinement": ("gameasg.initialization", ["--stage", "optimize", "--resume"]),
    "evolution": ("gameasg.rsi", []),
    "evaluate": ("gameasg", ["score"]),
    "environment": ("gameasg.environment", []),
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="play2evolve asg",
        description="GameASG browser games: Initialization → Refinement → Evolution",
        epilog="Refinement resumes the initialized workspace. Evolution uses --source and a new --output.")
    parser.add_argument("command", choices=list(COMMANDS))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command in ("initialization", "refinement", "evolution") and any(
        value == "--stage" or value.startswith("--stage=") for value in args.args
    ):
        parser.error("The public command selects its stage; omit --stage")
    module, prefix = COMMANDS[args.command]
    sys.argv = ["play2evolve asg " + args.command, *prefix, *args.args]
    runpy.run_module(module, run_name="__main__")


if __name__ == "__main__":
    main()
