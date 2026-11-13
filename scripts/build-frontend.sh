#!/usr/bin/env bash
set -euo pipefail
SUKA_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUKA_RUNTIME="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies"
if ! command -v node >/dev/null 2>&1 && [ -x "$SUKA_RUNTIME/node/bin/node" ]; then
  export PATH="$SUKA_RUNTIME/node/bin:$SUKA_RUNTIME/bin/fallback:$PATH"
fi
if ! command -v pnpm >/dev/null 2>&1; then
  echo "请先安装 Node.js 与 pnpm，或在 Codex 的依赖运行环境中执行。"
  exit 1
fi
cd "$SUKA_ROOT/frontend"
if [ ! -d node_modules ]; then
  pnpm install --frozen-lockfile --store-dir ../.cache/pnpm
fi
exec pnpm build

