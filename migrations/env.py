from alembic import context

from app.models import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("请使用 python -m app.cli migrate 执行迁移")
context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
with context.begin_transaction():
    context.run_migrations()

