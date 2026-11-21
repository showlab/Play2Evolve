# External dependencies

## Craft

GameCraft-Bench is an external project, not vendored here. The source checkout
used when extracting this implementation reported revision
`a43347534374df9a0c1a6c001aa9380862783f6d` and an Apache-2.0 license.
Install a compatible checkout separately and set `GAMECRAFT_BENCH_ROOT`.
Replay, frame sampling, and scoring use internal benchmark APIs, so changing
that dependency may require compatibility testing. The benchmark package and
its task files must be available on every execution host.

Godot, FFmpeg, Xvfb and xdotool are system dependencies. GPU video generation is
separate from Godot software rendering and from language-model calls.

The Python dependencies in `pyproject.toml` cover the HTTP clients, image
processing, and local pipeline. Optional local Wan execution additionally needs
an appropriate PyTorch/DiffSynth installation and weights. Configure
`WAN_PYTHON`, `WAN_DIFFSYNTH_PATH`, `WAN_MODEL_PATH`, and GPU settings explicitly.
These dependencies are intentionally not installed by the core package.

## GameASG

The HTML adapter requires the external GameASG-Bench checkout at revision
`6427cb6517ce11ff350ec45fcae17b67c5aec245` and its `gamebench-env:v2.0` Docker
image. The revision and unmodified tracked files are checked before runs.
The image provides the browser, Node.js and `ws`; these are not Python package
dependencies. The evaluator also requires a working Linux x86-64 Docker daemon.
GameASG does not require the Godot binary or GameCraft-Bench checkout.
See [GameASG guide](gameasg.md) for setup and scoring. Benchmark distribution
rights and acquisition instructions remain part of the release checklist.

## H3 service interface

See [H3 configuration and verification](h3.md) for environment variables,
per-stage precedence, a real clip-generation smoke test and deployment limits.

The default HTTP adapter uses:

- `POST /v1/videos`: submit a JSON request and receive a job `id`.
- `GET /v1/videos`: list jobs for Craft preflight.
- `GET /v1/videos/{id}`: poll until `completed` or `failed`.
- `GET /v1/videos/{id}/content`: download the MP4.

A text-to-video request includes `task: t2va`, `prompt`, `conditions`, `seed`,
and `target` (short edge, aspect ratio, duration). First/last-frame conditioning
uses `task: fl2va` and image conditions. Keep the service's supported model
variant, resolutions, and durations consistent with the requests.

The backend options `local`, `minimax`, and `wan` retain their existing adapters.
`local` means an H3-compatible endpoint, which may be on another machine;
`H3_URL` must resolve from the execution host. No H3 binaries, weights, private
server patches, or machine-specific launch scripts are included. Deploy a
compatible server separately and verify its license and model access terms.

## Model authentication

For the Codex backend, use the installed `codex` binary or set `CODEX_BIN` to your
own installation. Authenticate yourself. The research account-routing wrapper
and account fingerprints are not part of this repository.

For API calls, set the appropriate key in your shell. Custom Responses providers
remain explicit through `--codex-provider`, `--codex-base-url`, and
`--codex-api-key-env`. Never commit authentication stores or endpoint secrets.
