"""Separate prepare / demos / score stages. No optimizer or training entrypoint."""
import argparse
import os
import shutil
import time
from pathlib import Path

from gamecraft_behavior.core import write_json
from .packaging import check_frozen, check_implementation, package_demos, prepare


def godot_binary(value):
    value = value or os.getenv('GAMECRAFT_BENCH_GODOT_BIN') or 'godot'
    resolved = shutil.which(value)
    if not resolved:
        raise ValueError('Set --godot to an executable Godot binary')
    return str(Path(resolved).resolve())


def demos(run, godot):
    config = check_frozen(run)
    check_implementation(config)
    # Set transport flags before the lazy llm import. No H3 or optimizer calls.
    os.environ['LLM_BACKEND'] = config['player_backend']
    os.environ['OPTIMIZER_BACKEND'] = config['player_backend']
    os.environ['GAMECRAFT_BENCH_GODOT_BIN'] = godot
    os.environ.pop('BEHAVIOR_OBSERVE_ONLY', None)
    import llm
    from gamecraft_behavior.runtime import player, rollout
    from gamecraft_training.replay import verify
    decide = player(config['player_model'], config['player_backend'])
    directory = run / 'rollouts'
    directory.mkdir(exist_ok=False)
    before = dict(llm.USAGE)
    start = time.monotonic()
    results = []
    write_json(directory / 'configuration.json', {
        'godot': godot, 'scenario_seeds': config['seeds'], 'skill': '',
        'step_frames': config['step_frames'], 'max_decisions': config['max_decisions'],
        'backend': config['player_backend'], 'model_argument': config['player_model'],
        'checkpoint_label': config['player_checkpoint_label'],
        'local_model_note': 'local transport uses served alias local; record the deployed model separately',
        'retry_policy': 'at most one identical replay retry; never resample policy to select a better demo',
    })
    try:
        for seed in config['seeds']:
            destination = directory / f'seed-{seed}'
            result = rollout(run / 'initial-project', destination, '', decide, godot=godot,
                             step_frames=config['step_frames'], max_decisions=config['max_decisions'],
                             scenario=f'ready_seed_{seed}')
            if not result['replay_verified']:
                result = verify(result, destination, godot)
            if not result['replay_verified']:
                raise RuntimeError(f'Replay mismatch for seed {seed}; no demo was discarded or resampled')
            results.append(result)
        return package_demos(run, results)
    finally:
        write_json(directory / 'usage.json', {
            'player_usage': {k: llm.USAGE[k] - before[k] for k in before},
            'wall_seconds': time.monotonic() - start, 'completed_episodes': len(results),
            'replay_retry_episodes': sum(bool(r.get('replay_attempts')) for r in results),
            'scope': 'transport usage counters; parser retry calls may be undercounted by llm.py',
        })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare', help='Freeze project and protocol; no model or engine calls')
    p.add_argument('--project', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--seeds', type=int, nargs='+', default=[4000, 4001, 4002])
    p.add_argument('--step-frames', type=int, default=6)
    p.add_argument('--max-decisions', type=int, default=100)
    p.add_argument('--backend', choices=['local', 'api', 'codex'], default='local')
    p.add_argument('--model', default='local')
    p.add_argument('--player-label', help='Actual deployed checkpoint/version, especially for the local alias')
    d = sub.add_parser('demos', help='Empty-skill visual player and clean submission packaging')
    d.add_argument('--run', type=Path, required=True)
    d.add_argument('--godot')
    trained = sub.add_parser('trained-demos', help='Record final learned Player and package a new benchmark-ready project')
    trained.add_argument('--run', type=Path, required=True, help='Completed RSI run directory')
    trained.add_argument('--output', type=Path, required=True, help='New result directory; never overwrites the run')
    trained.add_argument('--bench', type=Path, required=True)
    trained.add_argument('--godot', required=True)
    trained.add_argument('--scenario', action='append', required=True,
                         help='Predeclared scenario ID; repeat for every planned Agent episode')
    trained.add_argument('--keep-demo', action='append', default=[],
                         help='Original demo filename to retain for complementary coverage')
    trained.add_argument('--step-frames', type=int, default=6)
    trained.add_argument('--max-decisions', type=int, default=100)
    trained.add_argument('--player-backend', choices=['local', 'api', 'codex'])
    trained.add_argument('--player-model')
    trained.add_argument('--player-mode', choices=['interactive', 'program'])
    trained.add_argument('--policy', type=Path, help='Optional previously authored program policy')
    trained.add_argument('--capture-mode', choices=['native', 'frame'],
                         help='Real-time input for program Player (default), or frame-stepped Player')
    showcase = sub.add_parser('rsi-showcase', help='Collect final selected RSI game coverage and benchmark-ready demos')
    showcase.add_argument('--run', type=Path, required=True)
    showcase.add_argument('--initialization-demos', type=Path,
                          help='Explicit final initialization demo_outputs directory')
    showcase.add_argument('--initialization-project', type=Path,
                          help='Project on which those initialization demos were recorded')
    showcase.add_argument('--output', type=Path, required=True, help='New directory outside the RSI run')
    showcase.add_argument('--bench', type=Path, required=True, help='Workspace GameCraft-Bench checkout')
    showcase.add_argument('--godot', required=True)
    showcase.add_argument('--step-frames', type=int, help='Defaults to the RSI run setting')
    showcase.add_argument('--player-backend', choices=['codex', 'api', 'local'])
    showcase.add_argument('--player-model')
    showcase.add_argument('--reasoning-effort', choices=['low', 'medium', 'high', 'xhigh'])
    showcase.add_argument('--showcase-mode', choices=['incremental', 'full', 'score_only',
                                                      'merged', 'preserve'],
                          default='incremental',
                          help='Reuse routes, rerecord goals, score existing routes, merge RSI goals, or preserve initialization video facts')
    showcase.add_argument('--showcase-parallelism', type=int, default=5,
                          help='Concurrent independent route checks and repairs in preserve mode (1–10)')
    showcase.add_argument('--use-working-project', action='store_true',
                          help='Score the last round-admitted RSI version, even if a legacy final review selected the original')
    from codex_provider import add_arguments as provider_arguments
    provider_arguments(showcase)
    showcase.set_defaults(codex_provider='openai')
    s = sub.add_parser('score', help='Official rubric scoring; calls the configured VLM judge')
    s.add_argument('--run', type=Path, required=True)
    s.add_argument('--bench', type=Path, required=True)
    s.add_argument('--godot')
    s.add_argument('--judge', choices=['openai', 'claude', 'opus', 'kimi', 'gemini'], required=True)
    s.add_argument('--judge-model', required=True)
    s.add_argument('--passes', type=int, default=1)
    s.add_argument('--timeout', type=int, default=1800)
    for command in ('score-videos', 'score-project'):
        direct = sub.add_parser(command, help='Score frozen inputs without running training')
        direct.add_argument('--bench', type=Path, required=True)
        direct.add_argument('--task', required=True, help='Official task ID, e.g. sports-boxing-gym')
        direct.add_argument('--output', type=Path, required=True)
        direct.add_argument('--judge', choices=['openai', 'claude', 'opus', 'kimi', 'gemini', 'codex'], required=True)
        direct.add_argument('--judge-model', required=True)
        direct.add_argument('--judge-label', help='Actual checkpoint behind an API alias')
        direct.add_argument('--input-label', default='existing frozen input; training provenance unspecified')
        direct.add_argument('--passes', type=int, default=3)
        direct.add_argument('--timeout', type=int, default=3600)
        if command == 'score-videos':
            direct.add_argument('--videos', type=Path, nargs='+', required=True,
                                help='Explicit videos; no best-demo selection or recursive discovery')
        else:
            direct.add_argument('--project', type=Path, required=True,
                                help='Clean Godot project with demo_outputs, not a training runtime copy')
            direct.add_argument('--godot')
    args = parser.parse_args()
    if args.command == 'prepare':
        print(prepare(args.project, args.output, seeds=args.seeds, step_frames=args.step_frames,
                      max_decisions=args.max_decisions, backend=args.backend, model=args.model,
                      player_label=args.player_label))
    elif args.command == 'demos':
        print(demos(args.run.resolve(), godot_binary(args.godot)))
    elif args.command == 'trained-demos':
        from .trained_demos import run as record_trained_demos
        print(record_trained_demos(run_dir=args.run, output=args.output, bench=args.bench,
              godot=Path(godot_binary(args.godot)), scenario_ids=args.scenario,
              keep_demos=args.keep_demo, step_frames=args.step_frames,
              max_decisions=args.max_decisions, backend=args.player_backend,
              model=args.player_model, mode=args.player_mode, policy_path=args.policy,
              capture_mode=args.capture_mode))
    elif args.command == 'rsi-showcase':
        from codex_provider import from_args
        from .rsi_showcase import run as record_rsi_showcase
        print(record_rsi_showcase(run_dir=args.run, output=args.output,
              bench=args.bench, godot=Path(godot_binary(args.godot)),
              backend=args.player_backend, model=args.player_model,
              step_frames=args.step_frames, provider=from_args(args),
              reasoning_effort=args.reasoning_effort, mode=args.showcase_mode,
              showcase_parallelism=args.showcase_parallelism,
              initialization_demos=args.initialization_demos,
              initialization_project=args.initialization_project,
              use_working_project=args.use_working_project))
    elif args.command in ('score-videos', 'score-project'):
        from .direct import run
        run(args)
    else:
        from .scoring import score
        result = score(args.run, args.bench, godot_binary(args.godot), args.judge,
                       args.judge_model, passes=args.passes, timeout=args.timeout)
        print(f"Overall: {result['overall_mean'] * 100:.2f}/100")
        print(args.run.resolve() / 'scoring/summary.json')


if __name__ == '__main__':
    main()
