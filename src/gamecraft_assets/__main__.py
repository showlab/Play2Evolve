import argparse
from pathlib import Path
from .plans import initialize, revise


def main():
    p = argparse.ArgumentParser(description='Dread Wings case design, H3 assets and Godot export')
    commands = p.add_subparsers(dest='command', required=True)
    init = commands.add_parser('plan')
    init.add_argument('--instruction', type=Path, required=True)
    init.add_argument('--output', type=Path, required=True)
    gen = commands.add_parser('generate')
    gen.add_argument('--workspace', type=Path, required=True)
    gen.add_argument('--only', nargs='+')
    gen.add_argument('--rounds', type=int, default=2)
    gen.add_argument('--candidates', type=int, default=2)
    rev = commands.add_parser('revise')
    rev.add_argument('--workspace', type=Path, required=True)
    rev.add_argument('--asset', required=True)
    rev.add_argument('--feedback', required=True)
    exp = commands.add_parser('export')
    exp.add_argument('--workspace', type=Path, required=True)
    exp.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.command == 'plan':
        print(initialize(a.instruction, a.output))
    elif a.command == 'revise':
        revise(a.workspace, a.asset, a.feedback)
    elif a.command == 'generate':
        import os
        os.environ['LLM_BACKEND'] = 'local'
        os.environ['OPTIMIZER_BACKEND'] = 'local'
        from .generate import generate
        pending = generate(a.workspace, a.only, a.rounds, a.candidates)
        print('Pending assets:', pending)
        if pending:
            raise SystemExit(2)
    else:
        from .export import export_game
        print(export_game(a.workspace, a.output))


if __name__ == '__main__':
    main()
