#!/usr/bin/env bash
# Run ytdj from its own venv — the system python refuses installs (PEP 668).
set -euo pipefail
cd "$(dirname "$0")"
# -u: unbuffered, so output is visible even when backgrounded (--web-only)
exec ./.venv/bin/python -u -m ytdj "$@"
