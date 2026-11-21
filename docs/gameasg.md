# GameASG operation guide

Start with the complete three-stage commands in [README](../README.md#run-gameasg-the-three-stages).
This backend produces self-contained browser HTML. Its browser evidence and
Docker evaluator are separate from Craft's Godot recording pipeline.
Configure and test the video endpoint using [the H3 guide](h3.md) before starting
the three-stage workflow.

## Required environment

- Linux x86-64, Python 3.12+, this installed package, FFmpeg and ffprobe.
- Codex CLI and an explicitly supplied, authenticated `--codex-home` directory.
  Pass executable paths from `command -v codex` and `command -v docker`; legacy
  helpers resolve these as file paths. The optional account hash has no default.
- An H3-compatible endpoint when the plan requests generated materials.
  `--asset-python` must point to the virtual-environment Python where this
  package is installed. Preserve its virtual-environment path, even if symlinked.
- GameASG-Bench revision `6427cb6517ce11ff350ec45fcae17b67c5aec245`, with unchanged
  tracked files, and Docker image `gamebench-env:v2.0`.
  The adapter verifies revision, Docker daemon and image before execution.

The benchmark/image is external; acquire it under its own license.
The image supplies Chromium, Node.js and the `ws` module for browser probes.
Installing Python dependencies alone does not install this browser runtime.
Generation reads public `target.md`, `game-spec.md`, and `tdd.md` documents;
official benchmark tests run only when scoring is explicitly requested.

Use `play2evolve asg environment check --bench "$GAMEASG_BENCH_ROOT"` for
preflight. `environment install` builds or imports the image when absent. To
reuse an image cache, add `--cache /path/to/image.tar.gz`; its accompanying JSON
manifest must match image identity, benchmark commit and archive hash.
Starting a private Docker daemon is an explicit opt-in; ordinary stage commands
do not start one. See `play2evolve asg environment --help` for options.

## Stage inputs and outputs

| Public stage | Input | Output and behavior |
| --- | --- | --- |
| Initialization | Benchmark public task documents | Plan, materials, composed HTML, structural validation, frozen initial export; no grouped playtest/Behavior Skill loop. |
| Refinement | Same initialized workspace and unchanged protocol arguments | Up to `groups × rounds` repairs, player policies, browser videos, issue diagnosis, candidate selection, skills, final export. |
| Evolution | Completed source workspace, new destination | Upgrade objective, full build, up to `rounds` repair rounds, retained HTML and final export. |

The launcher fixes the legacy `--stage` value. Passing your own `--stage` is
rejected to prevent accidentally executing another phase. Refinement forwards
`--resume` because it continues the initialized workspace. Evolution's three
rounds are repairs within one upgrade cycle, not a grouped nine-round run.

The implementation retains its checkpoint names:

- `initialization.json`: stage 1/2 protocol, selected version, per-group rounds,
  status, and coverage. It stays in the same workspace across both stages.
- `bootstrap-plan.json`, `versions/`, `optimization/`: plans, candidate versions,
  policies and browser evidence.
- `final-coverage.json`, `behavior-skills-final.json`: stage 2 final inspection
  and retained behavior knowledge when produced by the selected mode.
- `rsi.json` and `result.json`: stage 3 state and exported result. Read the result
  for actual artifact paths and build/repair status.
- `exports/bootstrap/game/index.html` and `exports/final/game/index.html`:
  frozen playable exports. `manifest.json` binds exports to document/HTML hashes.

`completed_with_coverage_gaps` differs from full evidence coverage; neither is an
official benchmark score. Inspect candidate/build failures in the checkpoint
before claiming an improvement.

## Resume an interrupted stage

Keep the Bash `ASG_COMMON` arguments from README unchanged. Protocols include
task, document hashes, model/provider, budgets, groups and image identity;
changing them can invalidate a resume. Preserve source workspaces and assets.

```bash
# Finish interrupted initial generation in place.
play2evolve asg initialization "${ASG_COMMON[@]}" \
  --output outputs/asg/garden-gulp/base --resume

# Finish interrupted refinement; --resume is supplied by the launcher.
play2evolve asg refinement "${ASG_COMMON[@]}" \
  --output outputs/asg/garden-gulp/base

# Finish the same interrupted evolution cycle.
play2evolve asg evolution "${ASG_COMMON[@]}" \
  --source outputs/asg/garden-gulp/base \
  --output outputs/asg/garden-gulp/evolution-01 --resume
```

Do not run two writers against one workspace. Existing lock and hash checks are
retained. Incomplete attempts may be retried and preserved as interrupted
directories; resume does not cache every unfinished model call.

## Run another evolution cycle

Change the source to the completed preceding cycle and use a new output:

```bash
play2evolve asg evolution "${ASG_COMMON[@]}" \
  --source outputs/asg/garden-gulp/evolution-01 \
  --output outputs/asg/garden-gulp/evolution-02
```

Use the whole source workspace. An HTML export alone lacks the checkpoint,
plans, selected-version hashes and retained skill state.

## Score frozen exports

The stage examples do not automatically score. After completion, pass the frozen
export directory to the scorer, not the raw candidate directory:

```bash
play2evolve asg evaluate outputs/asg/garden-gulp/base/exports/bootstrap \
  --exp-id garden-init-001 --docker "$(command -v docker)"
play2evolve asg evaluate outputs/asg/garden-gulp/base/exports/final \
  --exp-id garden-refine-001 --docker "$(command -v docker)"
play2evolve asg evaluate outputs/asg/garden-gulp/evolution-01/exports/final \
  --exp-id garden-evolve-001 --docker "$(command -v docker)"
```

Use unique experiment IDs. Add `--docker-host` and `--image` where necessary.
The scorer verifies the frozen HTML hash and writes official outputs under the
benchmark checkout's `output/EXP_ID/TASK/`. It copies reports into the export
directory and records evaluation in `manifest.json`. Inspect `summary-test.json`,
`report-l1.json`, `report-l2.json`, and `evaluation.log`.
An evaluated export cannot be scored again in place; prepare a fresh evaluation
workspace. The existing protection against overwriting results is retained.
L1/L2 results are not Craft's visual score.

## Migration boundary

Module names, checkpoint format strings, prompts, algorithms and recovery helpers
are retained. `gameasg.method` is a historical pilot whose Codex/probe helpers
are still imported; it is **not** the public three-stage workflow.
Use `play2evolve asg ...`. Model credentials, Docker images, benchmark task data
and experiment outputs are not distributed.
