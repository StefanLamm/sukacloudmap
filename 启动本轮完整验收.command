#!/usr/bin/env bash
set -euo pipefail
SUKA_TASK_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$SUKA_TASK_ROOT"
exec .venv/bin/python scripts/manage-services.py start history
