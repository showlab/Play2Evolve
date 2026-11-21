import argparse
import json
import os
import shutil
from pathlib import Path
from .core import train
from .runtime import player, reflector, rollout


def main():
    p = argparse.ArgumentParser(description='Adapter-driven screenshot Player and behavior skill optimization')
    p.add_argument('command', choices=['rollout', 'train'])
    source_group = p.add_mutually_exclusive_group(required=True)
    source_group.add_argument('--project', type=Path)
    source_group.add_argument('--game-package', type=Path, help='Frozen initialized game package; checks and copies before running')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--adapter', help='Trusted Python factory: package.module:factory; default inferred from project')
    p.add_argument('--scenario')
    p.add_argument('--player-state', choices=['pixels', 'engine'], default='pixels')
    p.add_argument('--skill', type=Path)
    p.add_argument('--backend', choices=['local', 'api', 'codex'], default='local')
    p.add_argument('--model', default='local')
    p.add_argument('--step-frames', type=int, default=6)
    p.add_argument('--max-decisions', type=int, default=40)
    p.add_argument('--rounds', type=int, default=1)
    p.add_argument('--godot', default=os.getenv('GAMECRAFT_BENCH_GODOT_BIN') or shutil.which('godot'))
    a = p.parse_args()
    if not a.godot: p.error('Godot is required')
    source, destination = (a.game_package or a.project).resolve(), a.output.resolve()
    if source == destination or source in destination.parents:
        p.error('Output must be outside the original project')
    if a.backend != 'local' and a.model == 'local': p.error('Set --model for api/codex')
    os.environ['LLM_BACKEND'] = a.backend
    os.environ['OPTIMIZER_BACKEND'] = a.backend
    skill = a.skill.read_text() if a.skill else ''
    from .adapters import resolve, load_factory
    if a.game_package:
        if a.adapter:
            p.error('--game-package already defines the adapter; do not override it')
        from game_initialization.package import checkout
        a.project, adapter, _ = checkout(a.game_package, a.output)
        a.output = a.output / 'behavior'
    else:
        adapter = resolve(a.project, load_factory(a.adapter) if a.adapter else None)
    decide = player(a.model, a.backend, adapter=adapter)
    def run(text, output):
        return rollout(a.project, output, text, decide, godot=a.godot,
                       step_frames=a.step_frames, max_decisions=a.max_decisions,
                       adapter=adapter, scenario=a.scenario, player_state=a.player_state)
    if a.command == 'rollout':
        r = run(skill, a.output)
        print(json.dumps({k: r[k] for k in ['replay', 'replay_verified', 'trace', 'video']}))
        if not r['replay_verified']: raise SystemExit(2)
    else:
        r = train(skill, a.output, run, reflector(a.model, a.backend, adapter=adapter), a.rounds, adapter=adapter)
        print(json.dumps({'accepted_rounds': sum(x['accepted'] for x in r['history']),
                          'best_skill': str(a.output / 'best_skill.txt'), 'replay': r['best']['replay']}))


if __name__ == '__main__':
    main()
