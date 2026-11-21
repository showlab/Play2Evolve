# Architecture and extraction boundary

The public launcher delegates to the existing module entry points:

| Command | Implementation |
| --- | --- |
| `initialization` | `game_initialization.agentic` |
| `refinement` | `gamecraft_training.package`, initialization profile |
| `evolution` | `gamecraft_training.package`, RSI profile |
| `showcase` | `gamecraft_evaluation rsi-showcase` |
| `evaluate` | `gamecraft_evaluation` |
| `package` | `game_initialization` |
| `asg initialization` | `gameasg.initialization --stage bootstrap` |
| `asg refinement` | `gameasg.initialization --stage optimize --resume` |
| `asg evolution` | `gameasg.rsi` |
| `asg evaluate` | `gameasg score` |
| `asg environment` | `gameasg.environment` |

## Stage naming

| Paper stage | Public command | Retained implementation terminology |
| --- | --- | --- |
| Game Initialization | `play2evolve initialization` | Bootstrap builder, `game_initialization` |
| Game Refinement | `play2evolve refinement` | Training profile `initialization`, `--initialization-*` |
| Game Evolution | `play2evolve evolution` | Training profile `rsi`, `--rsi-*` |

The preparation copy's old commands remain compatibility aliases:
`bootstrap` → `initialization`, `initialize` → `refinement`, and `rsi` → `evolution`.
In particular, old `initialize` still means stage 2; use the full noun
`initialization` for stage 1. Aliases print a migration hint.

Internal module paths, profile values, stored result fields, and lower-level
flags retain their names to keep saved records and the algorithm compatible.
This is a public naming change, not a change to stage order, rounds, prompts,
or selection logic. New workflow examples use stage names for output folders;
existing output folders are not renamed.

The builder prepares a package with a Godot project, adapter, scenarios, asset
plan, composition specification, and initialization metadata. Training checks out
private game copies; candidates and evidence are stored under each run's output.

Refinement and Evolution share the package-driven loop but select different
profiles. Player execution, issue diagnosis, planning, editing, skill updates,
and candidate comparison remain distinct roles. One role can make multiple model
calls. Do not infer a fixed call count from the number of roles.

Behavior Skill guides the player/policy. Video Skill stores material-generation
rules and boundary decisions. Neither changes model weights. The video-generation
client and code editor cooperate through the material tool and concrete asset
bindings. Retained historical helpers do not imply every legacy workflow is a
supported public entry point.

## Portability changes

- Preserved algorithm modules, prompt resources, Godot scripts, and JSON schemas.
- Added packaging, a stage dispatcher, environment configuration, and diagnostics.
- Replaced implicit sibling benchmark paths with a single configured resolver.
- Kept benchmark code selection consistent between replay, caching, and scoring.
- Replaced fixed host/tool/model-storage defaults with current-host, PATH,
  environment, or user-cache defaults.
- Made API client construction lazy so offline imports do not require credentials.
- Removed automatic lookup of a private image-key file; a key file must be explicit.
- Retained the reusable `gen_assets` utilities and their `game_spec` dependency;
  its per-game `main()` is not a public launcher.
- Excluded the obsolete top-level SkillOpt training entry and its worker/world
  modules; package-driven training is the release entry point.

No experiment outputs, account state, model weights, local virtual environments,
or independent Play2Code baseline are distributed. GameASG adapters are included;
the benchmark checkout and its Docker image remain external.

## Game package

The authoritative validation is in `game_initialization/package.py`. At minimum,
structural inspection expects `game/project.godot`, `game/game_plan.json`,
`adapter/__init__.py`, `asset_plan.json`, `composition_spec.json`, `scenarios.json`,
and `initialization.json`; training adds its own declared interfaces. Preserve
version metadata and recorded hashes when moving complete packages. Older
workspaces containing absolute paths or framework hashes are not automatically
migrated; prepare a fresh workspace using the new installation.
