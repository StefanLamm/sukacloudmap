#!/usr/bin/env bash
set -euo pipefail
SUKA_TASK_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$SUKA_TASK_ROOT"
exec "$SUKA_TASK_ROOT/.venv/bin/python" "$SUKA_TASK_ROOT/scripts/manage-services.py" start external
