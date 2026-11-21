#!/usr/bin/env bash
set -euo pipefail
python -m pytest -q "$@"
python -m build --no-isolation
