"""Bounded H3 material generation for an HTML GameASG workspace."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', type=Path)
    parser.add_argument('--feedback', help='Observed visual defect for a new candidate')
    parser.add_argument('--rounds', type=int, default=1,
                        help='Compatibility budget; integrated repairs generate one candidate per material')
    parser.add_argument('--only', nargs='+', help='Generate only these planned material IDs')
    parser.add_argument('--candidates', type=int, default=1)
    parser.add_argument('--commit-local-skill', action='store_true',
                        help='Compatibility flag; material generation alone does not approve a skill patch')
    parser.add_argument('--h3-steps', type=int, default=50,
                        help='H3 inference steps for this GameASG material run')
    args = parser.parse_args(argv)
    root = args.workspace.resolve()
    from gamecraft_assets.plans import validate_generic
    from gamecraft_assets.generate import generate
    from gamecraft_video_skill import initialize

    if not 1 <= args.h3_steps <= 50:
        raise ValueError('--h3-steps must be in [1, 50]')
    if not 1 <= args.rounds <= 3 or not 1 <= args.candidates <= 4:
        raise ValueError('Material rounds must be 1..3 and candidates 1..4')
    if args.h3_steps != 50:
        import gen_assets as g
        from gamecraft_assets.plans import write_json

        def stepped_h3_t2va(prompt, seed, out_mp4, aspect='16:9', seconds=4):
            request = {'task': 't2va', 'prompt': prompt, 'seed': seed,
                       'conditions': [], 'seconds': seconds,
                       'target': {'short_edge': 768, 'aspect_ratio': aspect,
                                  'duration_seconds': seconds},
                       'num_inference_steps': args.h3_steps}
            write_json(Path(out_mp4).parent / 'h3_request.json', request)
            return g.h3_video(request, out_mp4)

        g.h3_t2va = stepped_h3_t2va

    plan = json.loads((root / 'asset_plan.json').read_text())
    validate_generic(plan)
    if args.only and not set(args.only) <= {a['id'] for a in plan['assets']}:
        raise ValueError('--only references an unknown asset')
    initialize(root)
    # Match GameCraft's integrated Codex repair path: generate one readable
    # material, then decide whether to keep it after the game is rebuilt and
    # replayed. A standalone material verdict must not reject the whole repair.
    pending = generate(root, only=args.only, rounds=1,
                       candidates=1, validator=validate_generic,
                       judge=False, video_skill=True, phase='initialization',
                       feedback=args.feedback, auto_commit=False,
                       propose_video_patch=False)
    if pending:
        raise RuntimeError(f'H3 material generation failed: {pending}; inspect asset_history.json')
    manifest = json.loads((root / 'asset_manifest.json').read_text())
    asset = plan['assets'][0]['id']
    record = manifest['assets'][asset]
    print(json.dumps({'asset': asset, 'version': record['version'],
                      'files': record['files'], 'verdict': record['verdict'],
                      'assets': {aid: manifest['assets'][aid] for aid in
                                 (args.only or [a['id'] for a in plan['assets']])}}, indent=2))


if __name__ == '__main__':
    main()
