# Workflow

Run these commands in the installed environment. Use fresh output directories.
The model below is the inherited research default; change **all relevant model
flags** to a model available to your account when necessary.
Configure and smoke-test the video endpoint first using [the H3 guide](h3.md).

## 1. Game Initialization

```bash
play2evolve initialization prepare \
  --instruction examples/lake-flight/instruction.md \
  --workspace outputs/lake/initialization \
  --bootstrap-only --demo-only --model gpt-6-sol --video-skill \
  --h3-backend local --h3-url "$H3_URL" \
  --godot "$GAMECRAFT_BENCH_GODOT_BIN" \
  --asset-python "$(command -v python)"
play2evolve initialization run --workspace outputs/lake/initialization
```

`prepare` is offline and records the execution host, framework hash, and service
settings. Prepare on the host where `run` will execute, or supply `--host` with
that host's `socket.gethostname()` value. `run` calls real model services.
The builder's bounded checks and technical repairs are retained.
`--bootstrap-only` limits generation to stage 1, without automatically starting refinement.

Get the initialized package from the completed workspace's `state.json`:

```bash
INITIALIZED_PACKAGE=$(python - <<'PY'
import json
from pathlib import Path
root = Path('outputs/lake/initialization').resolve()
s = json.loads((root/'state.json').read_text())
assert s['stage'] == 'bootstrap_ready', s
p = Path(s['package'])
print(p if p.is_absolute() else root/p)
PY
)
```

## 2. Game Refinement: three groups, three rounds each

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

This selects the existing program-based grouping path. Fixed-trace, interactive
agent, and local-Qwen modes remain available through `--initialization-playtest`;
they do not all use identical grouping/entry semantics. `--rounds 3` is per group
in this refinement example, not three rounds total over the whole game.
Check `result.json` and final-showcase reports before using the output.

```bash
REFINED_PACKAGE=$(python - <<'PY'
import json
from pathlib import Path
r = json.loads(Path('outputs/lake/refinement/result.json').read_text())
print(r['package'])
PY
)
```

## 3. Game Evolution: three upgrade rounds

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

To continue a completed evolution run, replace `--game-package` with
`--continue-from-rsi outputs/lake/evolution` and use a new `--output` directory.
`--rsi-cycles` controls successive cycles; `--rounds` controls rounds per cycle.
These are existing pipeline capabilities, not a restart of Game Initialization.

History windows remain configurable with `--history-judge-rounds`,
`--history-legacy-planner-rounds`, `--history-planner-rounds`,
`--history-editor-rounds`, and `--history-skill-rounds`.

## Showcase and scoring

The benchmark-oriented standalone showcase command below requires a benchmark
task with a rubric. Use it for runs bootstrapped from a GameCraft task instruction;
the custom Lake Flight brief is for generation/optimization only. Refinement
also has its own final-showcase output inside the run.

```bash
play2evolve showcase \
  --run outputs/lake/evolution --output outputs/lake/showcase \
  --bench "$GAMECRAFT_BENCH_ROOT" --godot "$GAMECRAFT_BENCH_GODOT_BIN" \
  --showcase-mode preserve
```

This stage retains the existing source-package and coverage requirements. Read
its `result.json` for the exported project, recordings, and coverage status;
partial coverage and technical failure are not equivalent to a zero benchmark
score. Existing gate/configuration behavior has not been relaxed for this release.

The custom Lake Flight example has no official benchmark rubric. To score a
benchmark task, bootstrap from that task's public instruction instead, use its
exact task ID and the resulting final exported Godot project:

```bash
play2evolve evaluate score-project \
  --bench "$GAMECRAFT_BENCH_ROOT" --task YOUR_TASK_ID \
  --project /path/to/final/exported/game --output outputs/score \
  --judge codex --judge-model gpt-6-sol --passes 1 \
  --godot "$GAMECRAFT_BENCH_GODOT_BIN"
```

Keep task, judge model, demo selection, recording configuration, and aggregation
consistent when comparing initialization, refinement, and evolution scores. No score
is produced by a mocked test or a successful import alone.

The lower-level options `--bootstrap-only`, `--initialization-*`, `--rsi-*`,
`--continue-from-rsi`, and the `bootstrap_ready` state keep their serialized
names for compatibility. They map to stages 1, 2, and 3 respectively; see
[stage naming](architecture.md#stage-naming).
