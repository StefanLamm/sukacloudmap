"""Durable address lookup queue; no changes to business or historical rows."""
from alembic import op
import sqlalchemy as sa

revision = '0005_location_tasks'
down_revision = '0004_route_plans'
branch_labels = depends_on = None


def upgrade():
    op.add_column('system_state', sa.Column('import_revision', sa.Integer, nullable=False, server_default='1'))
    # Old preview stamps predate the distinction between user edits and address
    # enrichment. Require a fresh preview; committed batches remain untouched.
    op.execute("UPDATE master_imports SET master_revision=0 WHERE status='PREVIEW'")
    op.create_table('location_tasks', sa.Column('kind', sa.String(20), primary_key=True),
                    sa.Column('entity_id', sa.String(36), primary_key=True),
                    sa.Column('source_revision', sa.Integer, nullable=False), sa.Column('query', sa.String(400), nullable=False),
                    sa.Column('token', sa.String(36), nullable=False), sa.Column('status', sa.String(16), nullable=False),
                    sa.Column('reason', sa.String(200), nullable=False), sa.Column('lease_until', sa.BigInteger, nullable=False),
                    sa.Column('last_attempt_at', sa.BigInteger, nullable=False),
                    sa.Column('created_by', sa.String(36), sa.ForeignKey('accounts.id'), nullable=False),
                    sa.Column('created_at', sa.BigInteger, nullable=False))
    op.create_index('ix_location_tasks_status', 'location_tasks', ['status'])
    op.create_index('ix_location_tasks_last_attempt_at', 'location_tasks', ['last_attempt_at'])


def downgrade():
    op.drop_table('location_tasks')
    op.drop_column('system_state', 'import_revision')
