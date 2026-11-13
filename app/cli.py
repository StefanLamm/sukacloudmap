import argparse
import getpass
import os

from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.database import SCHEMA_HEAD, make_engine, migrate, session_factory, start_write
from app.models import Account, AuditEvent
from app.schemas import CreateAccount
from app.security import hash_password


def main():
    parser = argparse.ArgumentParser(description="苏咔云图数据库与内部管理员初始化")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="迁移数据库至当前版本")
    init = commands.add_parser("init-admin", help="创建首位管理员；不会覆盖现有账号")
    init.add_argument("--username", default="admin")
    init.add_argument("--display-name", default="系统管理员")
    args = parser.parse_args()
    engine = make_engine(Settings())
    try:
        migrate(engine)
        if args.command == "migrate":
            print("数据库迁移完成：" + SCHEMA_HEAD)
            return
        secret = os.environ.get("SUKA_BOOTSTRAP_PASSWORD")
        if not secret:
            secret = getpass.getpass("管理员密码（12至128位，不回显）：")
            if secret != getpass.getpass("再次输入密码："):
                parser.exit(1, "两次密码不一致，未创建账号。\n")
        try:
            payload = CreateAccount(username=args.username, display_name=args.display_name, password=secret, is_admin=True)
        except ValidationError:
            parser.exit(1, "账号格式、姓名或密码长度不符合要求，未创建账号。\n")
        with session_factory(engine)() as db:
            start_write(db, engine)
            if db.scalar(select(Account.id).where(Account.is_admin.is_(True))):
                parser.exit(1, "管理员已存在，请通过账号管理创建其他账号；不会覆盖已有密码。\n")
            if db.scalar(select(Account.id).where(Account.username == payload.username)):
                parser.exit(1, "账号已存在，未覆盖。\n")
            account = Account(username=payload.username, display_name=payload.display_name, password_hash=hash_password(secret), is_admin=True)
            db.add(account)
            db.flush()
            db.add(AuditEvent(actor_id=account.id, action="ADMIN_INITIALIZED", entity_id=account.id, summary="初始化首位管理员"))
            db.commit()
            print(f"管理员 {account.username} 已创建。密码不在代码、日志或输出中保存。")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

