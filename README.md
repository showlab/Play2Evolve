# Play2Evolve

### Playfulness as Feedback for Agentic Game Development

**Video-generated worlds. Programmable gameplay. Improvement through play.**

[Project page & video demos](https://play2evolve-project-preview.steadydrake257.chatgpt.site) ·
[Installation](#installation) · [Craft quickstart](#craft-quickstart) ·
[GameASG quickstart](#gameasg-quickstart) · [Documentation](#documentation)

## TL;DR

**Play2Evolve builds playable games by combining video generation with game code,
then uses agent playtesting to evolve both together.** Video models generate
appearance and motion; code provides interaction, rules, and game state.
Player, Judge, and Designer agents turn gameplay evidence into coordinated
changes to visual materials and executable logic.

- **Build:** turn a game brief into a runnable game with generated visual materials.
- **Refine:** playtest, diagnose defects, and repair the visuals and code together.
- **Evolve:** propose new gameplay and aesthetic goals, implement upgrades, and test the next version.

This repository contains two backends: **Craft for Godot games** and **GameASG for
browser games**, exposed through the `play2evolve` CLI. The
[project page](https://play2evolve-project-preview.steadydrake257.chatgpt.site)
shows recorded gameplay, side-by-side checkpoints, and successive evolution demos;
it does not run the games in your browser. Paper and public code release details are forthcoming.

## Overview

<p align="center">
  <a href="docs/media/teaser.png">
    <img src="docs/media/teaser.png" alt="Play2Evolve: Player, Judge, and Designer agents coordinate video generation and code editing using playtest feedback" width="100%">
  </a>
</p>

**One feedback loop updates both materials and code.** For example, replacing a
still character with an animation also requires changes to frame playback,
placement, and responses to player input. Playtesting evaluates the integrated game.

### Reported results

Overall GameCraft-Bench scores from the current manuscript:

| Method / checkpoint | Overall score |
| --- | ---: |
| Initialization (V0) | 50.81 |
| Play2Code | 52.46 |
| Refinement (V1) | 54.45 |
| Evolution (V2) | **60.05** |

Evolution improves the overall score by **9.24 points** over initialization.
See the [full results table](https://play2evolve-project-preview.steadydrake257.chatgpt.site/#results)
for per-dimension scores. These manuscript results are separate from the
[release checks](docs/validation.md); selected demo clips do not define the benchmark cohort.

## How it works

| Stage | What happens | Output |
| --- | --- | --- |
| Initialization | A builder turns a public game brief into executable code and generated materials. | A runnable game package. |
| Refinement | Agents playtest scenario groups, diagnose defects, implement candidate repairs, and compare replays. | A refined game and playtest reports. |
| Evolution | A designer proposes gameplay or aesthetic upgrades; further playtests guide implementation and selection. | An evolved game checkpoint. |

<p align="center">
  <a href="docs/media/pipeline.png">
    <img src="docs/media/pipeline.png" alt="Initialization, refinement, and evolution pipeline, with Behavior Skills and adaptive video-generation levels" width="100%">
  </a>
</p>

- **Behavior Skills** carry experience from earlier play into later exploration.
- **Video Skill** is a separate component for material generation and integration.
- **Evaluation** exports demonstrations and invokes an external benchmark evaluator;
  internal candidate selection is distinct from benchmark scoring.

The examples below use three playtest groups with up to three repair rounds per
group for refinement. An evolution cycle uses one upgrade with up to three repair
rounds. The retained `gamecraft_*` modules implement these stages; the public CLI
dispatches to them. See [stage naming](docs/architecture.md#stage-naming) for the
mapping to legacy names.

## Choose a backend

| Backend | Game runtime | Three stage commands | Scoring |
| --- | --- | --- | --- |
| Craft (default) | Godot | `initialization`, `refinement`, `evolution` | GameCraft-Bench |
| GameASG | Browser, self-contained HTML | `asg initialization`, `asg refinement`, `asg evolution` | GameASG-Bench Docker L1/L2 |

Both adapters retain their existing execution and selection logic. Browser
playtesting does not use the Godot recorder. Only install the system dependencies
for the backend you intend to run.

## Installation

Install the package, configure the dependencies for your chosen backend, then
follow its three-stage quickstart. Material generation requires a running
H3-compatible service; installing this package does not deploy one.

Linux and Python 3.12+ are required. Craft needs Godot 4, FFmpeg/ffprobe,
Xvfb and xdotool on `PATH`. Recorded OS-input evaluation uses X11. GameASG
needs Linux x86-64, a working Docker daemon, FFmpeg and the pinned benchmark
image (see below); its browser and Node.js run inside that image.
Install Codex CLI and authenticate using your own account if selecting the Codex
backend. No credentials or model weights are included.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

For **Craft**, install GameCraft-Bench separately from a checkout you are authorized to use:

```bash
export GAMECRAFT_BENCH_ROOT=/absolute/path/to/GameCraft-Bench
python -m pip install -e "$GAMECRAFT_BENCH_ROOT"
export GAMECRAFT_BENCH_GODOT_BIN=/absolute/path/to/godot
export H3_URL=http://127.0.0.1:30010
export LLM_BACKEND=codex
export OPTIMIZER_BACKEND=codex
play2evolve doctor
```

The benchmark is also used by replay and final-showcase code. Keep the same
checkout throughout a run. `docs/dependencies.md` records the source revision
used during extraction. A functioning H3 service is needed for actual material
generation; Play2Evolve does not start or replace GPU services automatically.

## Configure H3 video generation

Start or obtain an H3-compatible HTTP service **before** running generation.
This repository contains the client, not the H3 weights, inference environment
or serving deployment. The service may run on the same host or a remote GPU host.
The URL must be reachable from the machine executing the asset Python process;
`127.0.0.1` refers to that machine, not your laptop or development machine.

```bash
export H3_BACKEND=local
export H3_URL=http://127.0.0.1:30010  # base URL, without /v1 or a trailing slash
export GAMECRAFT_H3_JOB_TIMEOUT_SECONDS=3600
export GAMECRAFT_ASSET_PYTHON="$(command -v python)"

# Run on the actual execution host; these checks do not generate video.
curl --noproxy '*' --fail --max-time 10 "$H3_URL/health"
curl --noproxy '*' --fail --max-time 10 "$H3_URL/v1/videos"
```

`local` is the name of the HTTP adapter; it can connect to a remote server.
The service must implement this project's request schema and job endpoints;
an arbitrary SGLang endpoint or hosted video API is not automatically compatible.
For an existing remote service, replace the host in `H3_URL`. GPU count, model
placement and distributed inference are **server-side** settings. Eight-card
inference cannot be enabled by changing the client URL or asset parallelism.

| Setting | How to use it |
| --- | --- |
| `H3_URL` | Base address for the `local` adapter. Craft Initialization records it at `prepare`; Refinement/Evolution inherit the environment. |
| `--h3-url` | Explicit address for Craft Initialization and GameASG commands. GameASG defaults to localhost, so pass `--h3-url "$H3_URL"`. |
| `--h3-backend local` | Explicit Craft backend selection. The GameASG integrated material worker currently uses `local`. |
| `GAMECRAFT_H3_JOB_TIMEOUT_SECONDS` | Craft material job polling budget, including server queue time; default 1200 seconds. |
| `--h3-timeout` | GameASG material timeout; default 3600 seconds. The worker exports it as the effective job timeout. |
| `--asset-python` | Python with Play2Evolve and media dependencies installed, able to reach the service. It need not load H3 weights for the HTTP backend. |
| `--asset-parallelism 1 --asset-candidates 1` | Craft repair candidate budgets, not the number of inference GPUs or a global retry limit. |

Configure the H3 server's default to **50 inference steps** for these examples.
Craft's default requests omit the step count and use that server default.
GameASG's `--h3-steps 50` also uses the server default in the retained worker;
nondefault values override text-to-video requests. Confirm the effective steps
from server configuration/logs rather than assuming a client flag enforces them.

`play2evolve doctor` checks local tools/imports, not H3 generation. Before a long
run, follow [the H3 guide](docs/h3.md) to generate a single clip, verify the MP4,
and check timeout, conditioning-file and multi-GPU requirements. For a custom
endpoint requiring authentication, the current local adapter needs an appropriate
adapter/proxy; it does not send bearer credentials by default.

<a id="craft-quickstart"></a>

## Run Craft: the three stages

Run from this repository root after installation. Replace model names if your
account uses another model; keep the player, judge and editor choices explicit.
Each command below calls real services except `initialization prepare`.

### 1. Initialization — generate the game

```bash
play2evolve initialization prepare \
  --instruction examples/lake-flight/instruction.md \
  --workspace outputs/lake/initialization --bootstrap-only --demo-only \
  --model gpt-6-sol --video-skill \
  --h3-backend local --h3-url "$H3_URL" \
  --godot "$GAMECRAFT_BENCH_GODOT_BIN" --asset-python "$(command -v python)"
play2evolve initialization run --workspace outputs/lake/initialization
```

`--bootstrap-only` keeps this stage separate from refinement. After it finishes,
read the actual package path (do not pass the workspace itself as a package):

```bash
INITIALIZED_PACKAGE=$(python - <<'PYCODE'
import json
from pathlib import Path
root = Path('outputs/lake/initialization').resolve()
s = json.loads((root / 'state.json').read_text())
assert s['stage'] == 'bootstrap_ready', s['stage']
p = Path(s['package'])
print(p if p.is_absolute() else root / p)
PYCODE
)
```

### 2. Refinement — three groups, three rounds per group

```bash
play2evolve refinement \
  --game-package "$INITIALIZED_PACKAGE" --output outputs/lake/refinement \
  --rounds 3 --initialization-playtest program --initialization-group-count 3 \
  --initialization-behavior-skill --player-state engine \
  --player-backend codex --player-model gpt-6-sol \
  --judge-backend codex --judge-model gpt-6-sol \
  --composition-mode codex --codex-model gpt-6-sol \
  --video-skill --asset-candidates 1 \
  --godot "$GAMECRAFT_BENCH_GODOT_BIN" --asset-python "$(command -v python)"
```

This is up to nine repair rounds across three playtest groups. Review the final
reports, then obtain the next input from `result.json`:

```bash
REFINED_PACKAGE=$(python - <<'PYCODE'
import json
from pathlib import Path
r = json.loads(Path('outputs/lake/refinement/result.json').read_text())
print(r['package'])
PYCODE
)
```

### 3. Evolution — one upgrade cycle with three repair rounds

```bash
play2evolve evolution \
  --game-package "$REFINED_PACKAGE" --output outputs/lake/evolution \
  --rounds 3 --rsi-mode upgrade --player-mode program --player-state engine \
  --player-backend codex --player-model gpt-6-sol \
  --judge-backend codex --judge-model gpt-6-sol \
  --composition-mode codex --codex-model gpt-6-sol \
  --video-skill --asset-candidates 1 \
  --godot "$GAMECRAFT_BENCH_GODOT_BIN" --asset-python "$(command -v python)"
```

For another evolution cycle, use `--continue-from-rsi outputs/lake/evolution`
instead of `--game-package`, with a **new** output directory. This is distinct
from refinement's three groups. See [Craft workflow](docs/workflow.md) for
showcase export, benchmark scoring and additional options. Lake Flight is a
custom example without an official benchmark rubric; official scoring requires
a benchmark task's public instruction and exact task ID.

<a id="gameasg-quickstart"></a>

## Run GameASG: the three stages

Obtain a GameASG-Bench checkout separately. The adapter requires revision
`6427cb6517ce11ff350ec45fcae17b67c5aec245` and unchanged tracked files.
It is not bundled here. With Docker already running:

```bash
export GAMEASG_BENCH_ROOT=/absolute/path/to/GameASG-Bench
play2evolve asg environment install --bench "$GAMEASG_BENCH_ROOT"
play2evolve asg environment check --bench "$GAMEASG_BENCH_ROOT"
```

`install` reuses or builds `gamebench-env:v2.0`; it can take time and download
image dependencies. It does not start a daemon unless explicitly requested.
Craft's `doctor` is not the environment check for this backend.

Configure the shared arguments in **Bash**, using your own authenticated Codex
home. Run all three stages in this shell; keep these values unchanged when
resuming a workspace:

```bash
export ASG_CODEX_HOME=/absolute/path/to/your/authenticated/codex-home
export H3_URL=http://127.0.0.1:30010
ASG_COMMON=(
  --bench "$GAMEASG_BENCH_ROOT" --task garden-gulp
  --codex-home "$ASG_CODEX_HOME" --codex "$(command -v codex)"
  --codex-provider openai --model gpt-6-sol
  --asset-python "$(command -v python)" --h3-url "$H3_URL"
  --h3-steps 50 --h3-timeout 3600
  --docker "$(command -v docker)" --image gamebench-env:v2.0
  --groups 3 --rounds 3 --play-seconds 120 --max-actions 240
)
```

The asset Python must have this package installed. No project-specific account
is selected by default; `--expected-account-hash` is an optional identity check.
For a nondefault Docker daemon, also add `--docker-host YOUR_DAEMON_URI` to the
shared arguments and the environment commands.

```bash
# 1. Initialization: generate and export the initial HTML game.
play2evolve asg initialization "${ASG_COMMON[@]}" \
  --output outputs/asg/garden-gulp/base

# 2. Refinement: resume THAT SAME workspace, three rounds per group.
play2evolve asg refinement "${ASG_COMMON[@]}" \
  --output outputs/asg/garden-gulp/base

# 3. Evolution: take the completed workspace, write a NEW cycle directory.
play2evolve asg evolution "${ASG_COMMON[@]}" \
  --source outputs/asg/garden-gulp/base \
  --output outputs/asg/garden-gulp/evolution-01
```

Run each stage only after the preceding command succeeds and its reports have
been inspected. Initialization generates and validates the package; grouped
playtesting and Behavior Skill updates begin in refinement. Each evolution
cycle builds an upgrade, then runs up to three repair rounds, not three groups.

| Stage | Checkpoint | Playable export |
| --- | --- | --- |
| Initialization | `base/initialization.json` (`bootstrapped`) | `base/exports/bootstrap/game/index.html` |
| Refinement | `base/initialization.json` | `base/exports/final/game/index.html` |
| Evolution | `evolution-01/rsi.json`, `result.json` | `evolution-01/exports/final/game/index.html` |

Checkpoint and export names retain their legacy spelling. A completed run can
report coverage gaps; completion alone is not a benchmark pass. Evolution's
`--source` needs the **whole workspace**, not `exports/final` or an HTML file.
See [GameASG operation guide](docs/gameasg.md) for interrupted-run recovery,
additional evolution cycles, scoring, and output inspection.

## Configuration and compatibility

Copy `configs/environment.example.toml` to `configs/local.toml` and replace
paths to use `play2evolve --config configs/local.toml COMMAND ...`. Existing
environment variables take precedence. The GameASG examples pass service and
tool settings explicitly; keep required stage arguments even when using TOML.
Keep credentials in the model client's login store or environment, not TOML.

The old `bootstrap`, `initialize`, and `rsi` commands remain aliases for
`initialization`, `refinement`, and `evolution`. Internal flags such as
`--initialization-playtest` and `--rsi-mode` retain their meaning. See
[stage naming](docs/architecture.md#stage-naming) before using historical scripts.

## Documentation

| If you want to… | Start here |
| --- | --- |
| Watch gameplay and compare versions | [Project page](https://play2evolve-project-preview.steadydrake257.chatgpt.site) |
| Run Craft, export showcases, or score a game | [Craft workflow](docs/workflow.md) |
| Run or resume the browser-game pipeline | [GameASG guide](docs/gameasg.md) |
| Connect and validate video generation | [H3 service guide](docs/h3.md) |
| Understand stages and internal modules | [Architecture](docs/architecture.md) |
| Check external tools and pinned revisions | [Dependencies](docs/dependencies.md) |
| Review tested behavior and release limitations | [Validation](docs/validation.md) · [Release checklist](docs/release_checklist.md) |

## Repository

- `src/game_initialization/`: requirements, builder tools, game-package construction.
- `src/gamecraft_training/package/`: refinement, evolution, candidate selection, final showcase.
- `src/gamecraft_behavior/`: players, reactive policies, Godot bridge, input recording/replay.
- `src/gamecraft_assets/` and `src/gamecraft_composition/`: material generation and game integration.
- `src/gamecraft_video_skill/`: generation rules and material-boundary decision memory.
- `src/gamecraft_evaluation/`: replay collection, showcase coverage, benchmark adapters.
- `src/gameasg/`: HTML generation, grouped refinement, evolution, browser playtesting and Docker scoring.
- `src/play2evolve/`: portable configuration and CLI.
- `tests/`: retained GameCraft tests and release-portability checks.

Source provenance is recorded in `docs/source_snapshot.json`. Original game
outputs, private account routing, model environments, and Play2Code
baselines are excluded. Historical per-game SkillOpt entry points are not part
of this release; some example/adaptation helpers remain as internal dependencies.

## Verification

```bash
python -m pytest -q
python -m build
```

Tests use mocks or fixtures for model calls. Engine integration tests require
Godot and the benchmark; one browser policy test requires host Node.js. Neither
the full live Godot pipeline nor the live Docker pipeline was run for this release; consult the validation report for skipped or failing
checks. Game packages contain executable adapter code and should come from
trusted sources.

## Release status and license

**Release preparation:** the source has been extracted from a research workspace.
See [validation](docs/validation.md) for checks actually performed and
[release checklist](docs/release_checklist.md) for remaining publication work.
No open-source license has been selected yet; this preparation copy does not grant
redistribution rights beyond those already held by the respective owners.
