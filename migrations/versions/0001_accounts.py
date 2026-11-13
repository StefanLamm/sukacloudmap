"""账号、数据库会话、共享记录与审计。

Revision ID: 0001_accounts
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_accounts"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("system_state", sa.Column("id", sa.Integer, primary_key=True), sa.Column("revision", sa.Integer, nullable=False))
    op.bulk_insert(sa.table("system_state", sa.column("id", sa.Integer), sa.column("revision", sa.Integer)), [{"id": 1, "revision": 1}])
    op.create_table("accounts",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(80), nullable=False), sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("is_admin", sa.Boolean, nullable=False), sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("auth_revision", sa.Integer, nullable=False), sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False), sa.Column("updated_at", sa.BigInteger, nullable=False),
        sa.Column("last_login_at", sa.BigInteger, nullable=True), sa.CheckConstraint("revision > 0", name="ck_account_revision"))
    op.create_table("login_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True), sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False), sa.Column("auth_revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False), sa.Column("expires_at", sa.BigInteger, nullable=False))
    op.create_index("ix_login_sessions_account_id", "login_sessions", ["account_id"])
    op.create_index("ix_login_sessions_expires_at", "login_sessions", ["expires_at"])
    op.create_table("login_throttles", sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer, nullable=False), sa.Column("window_start", sa.BigInteger, nullable=False))
    op.create_table("shared_records",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("title", sa.String(120), nullable=False), sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("updated_by", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False), sa.Column("created_at", sa.BigInteger, nullable=False), sa.Column("updated_at", sa.BigInteger, nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_record_revision"))
    op.create_table("audit_events",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("actor_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("action", sa.String(64), nullable=False), sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("summary", sa.String(240), nullable=False), sa.Column("occurred_at", sa.BigInteger, nullable=False))
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])


def downgrade():
    for name in ["audit_events", "shared_records", "login_throttles", "login_sessions", "accounts", "system_state"]:
        op.drop_table(name)

