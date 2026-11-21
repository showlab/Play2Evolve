# Extraction validation

Validated on 2026-11-20 against source commit
`85a699d57be92fb34374f91c3cedfca387a0ba05`.

## Completed checks

- Combined Craft, GameASG and portability tests: **522 passed, 11 skipped** (35.64 s).
  Tests were run in the release copy with the external benchmark checkout
  configured. Model calls were mocked; this was not a live training run.
- Ten skipped tests require Godot for real-engine execution; one browser policy
  test requires host Node.js. These are not successful game-rendering checks.
- Built a source archive and wheel using `python -m build --no-isolation`.
- Installed the wheel into a separate directory and tested from an unrelated
  working directory with Python isolated mode and no model credentials.
  Twelve runtime modules imported from that installation; four packaged
  resources and the Bootstrap framework hash were checked.
- All six stage commands (original aliases `bootstrap`, `initialize`, `rsi`, `showcase`,
  `evaluate`, `package`) displayed help from the installed wheel.
- Offline Bootstrap preparation produced a workspace with `bootstrap_only`
  enabled. This step prepares inputs; it does not generate or run a game.
- After adding GameASG, independently installed the updated wheel and checked
  its imports, all three JavaScript resources, and all five `asg` commands'
  help from an unrelated working directory with Python isolated mode.
- Syntax-checked 22 Bash examples in README and operation guides.
- SHA-256 comparison of 292 tracked files in the original source repository
  found no changes. Its working tree remained clean.

## Reproducing the tests

Install this project with its `dev` dependencies and configure/install the
external benchmark as described in the README. Then run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
python -m build --no-isolation
```

The plugin setting avoids unrelated pytest plugins installed on a shared host.
Tests include a local socket-based bridge mock and require permission to create
local sockets. FFmpeg should be available on `PATH` for media-related tests.

## Scope and remaining validation

Tests check canonical commands and compatibility aliases, argument forwarding,
and rejection of conflicting stage/profile overrides. The three canonical
Craft commands and the GameASG commands displayed help using their actual
underlying entry points. The full-suite count includes both naming and GameASG
packaging changes; the underlying optimization algorithms were retained.

Portability changes cover environment configuration, dependency discovery,
packaging, and command dispatch. Existing module names and optimization logic
are retained. Migrated test fixtures were updated for the copied implementation.
The adapted source-file list is recorded in `source_snapshot.json`.

A live Initialization → Refinement → Evolution run has **not** been performed
in this release copy for either backend. Craft needs Godot/X11 and GameCraft-Bench;
GameASG needs the pinned benchmark and Docker browser image. Both require an
authorized model backend and an H3 service for generated materials. No existing
training job, model service, or original source file was changed for this extraction.

This is a release-preparation snapshot. License selection and the other items in
[the publication checklist](release_checklist.md) remain before public release.
