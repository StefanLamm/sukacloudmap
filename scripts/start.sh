#!/usr/bin/env bash
set -euo pipefail
SUKA_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SUKA_ROOT"
SUKA_PYTHON="$SUKA_ROOT/.venv/bin/python"
if [ ! -x "$SUKA_PYTHON" ]; then
  echo "请先按 README 安装 Python 依赖。"
  exit 1
fi
if [ ! -f frontend/dist/index.html ]; then
  echo "前端尚未构建，请先执行 scripts/build-frontend.sh。"
  exit 1
fi
"$SUKA_PYTHON" -m app.cli migrate
if ! "$SUKA_PYTHON" - <<'PY'
import sys
from sqlalchemy import select
from app.config import Settings
from app.database import make_engine, session_factory
from app.models import Account
engine = make_engine(Settings())
with session_factory(engine)() as db:
    exists = db.scalar(select(Account.id).where(Account.is_admin.is_(True)))
engine.dispose()
sys.exit(0 if exists else 1)
PY
then
  echo "首次启动，请设置管理员密码。账号为 admin，输入不会回显。"
  "$SUKA_PYTHON" -m app.cli init-admin
fi
echo "本地应用：http://127.0.0.1:8000  按 Ctrl+C 停止。"
exec "$SUKA_PYTHON" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log

