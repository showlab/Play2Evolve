"""Run from code_world: python -m gamecraft_composition --help."""
import argparse
import json
import os
import shutil
from pathlib import Path

from .core import optimize
from .runtime import Evaluator, proposer


def main():
    p = argparse.ArgumentParser(description='Independent Godot composition RSI; never modifies the input project.')
    p.add_argument('--project', type=Path, required=True)
    p.add_argument('--feedback', type=Path, required=True, help='Internal rollout feedback, NOT the official benchmark rubric')
    p.add_argument('--output', type=Path, required=True, help='New directory outside the input project')
    p.add_argument('--steps', type=int, default=1)
    p.add_argument('--patch', type=Path, help='Replay one prepared patch without calling a proposer model')
    p.add_argument('--backend', choices=['local', 'api', 'codex'], default='local')
    p.add_argument('--model', default='local')
    p.add_argument('--judge', default='openai')
    p.add_argument('--judge-model')
    p.add_argument('--allow-stub', action='store_true', help='Infrastructure smoke test only; not optimization evidence')
    p.add_argument('--godot', default=os.getenv('GAMECRAFT_BENCH_GODOT_BIN') or shutil.which('godot'))
    p.add_argument('--timeout', type=int, default=600)
    a = p.parse_args()
    if not a.godot:
        p.error('Set --godot or GAMECRAFT_BENCH_GODOT_BIN')
    if a.judge == 'stub' and not a.allow_stub:
        p.error('Stub scoring is only allowed with --allow-stub for infrastructure checks')
    if a.patch and a.steps != 1:
        p.error('--patch requires --steps 1')
    if a.backend != 'local' and a.model == 'local' and not a.patch:
        p.error('Set --model for an api/codex proposer')
    os.environ['GAMECRAFT_BENCH_GODOT_BIN'] = a.godot
    if not a.patch:
        # Avoid initializing an unrelated API client in the existing llm module.
        os.environ['LLM_BACKEND'] = a.backend
        os.environ['OPTIMIZER_BACKEND'] = a.backend
    feedback = json.loads(a.feedback.read_text())
    propose = (lambda *_: json.loads(a.patch.read_text())) if a.patch else proposer(a.model, a.backend)
    result = optimize(a.project, a.output, feedback, propose,
                      Evaluator(a.godot, a.judge, a.judge_model, a.timeout), a.steps)
    print(json.dumps({'accepted_project': result['accepted_project'], 'accepted_steps': result['accepted_steps'],
                      'failed_steps': result['failed_steps']}))
    if result['failed_steps']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
