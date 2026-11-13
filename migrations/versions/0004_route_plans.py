"""Durable bounded planning jobs, road cache, immutable plan versions."""
from alembic import op
import sqlalchemy as sa

revision = '0004_route_plans'
down_revision = '0003_daily_inputs'
branch_labels = depends_on = None


def upgrade():
    op.create_table('road_cache', sa.Column('key',sa.String(64),primary_key=True),sa.Column('content',sa.Text,nullable=False),sa.Column('expires_at',sa.BigInteger,nullable=False))
    op.create_index('ix_road_cache_expires_at','road_cache',['expires_at'])
    op.create_table('planning_jobs',sa.Column('id',sa.String(36),primary_key=True),sa.Column('delivery_date',sa.String(10),nullable=False),
                    sa.Column('snapshot_id',sa.String(36),sa.ForeignKey('input_snapshots.id'),nullable=False),sa.Column('kind',sa.String(16),nullable=False),
                    sa.Column('status',sa.String(20),nullable=False),sa.Column('request',sa.Text,nullable=False),sa.Column('result',sa.Text),
                    sa.Column('error_code',sa.String(64)),sa.Column('error_message',sa.String(240)),sa.Column('cancel_requested',sa.Boolean,nullable=False),
                    sa.Column('lease_until',sa.BigInteger),sa.Column('created_by',sa.String(36),sa.ForeignKey('accounts.id'),nullable=False),
                    sa.Column('created_by_name',sa.String(80),nullable=False),sa.Column('created_at',sa.BigInteger,nullable=False),sa.Column('started_at',sa.BigInteger),sa.Column('finished_at',sa.BigInteger))
    op.create_index('ix_planning_jobs_delivery_date','planning_jobs',['delivery_date']); op.create_index('ix_planning_jobs_status','planning_jobs',['status'])
    op.create_table('plan_versions',sa.Column('id',sa.String(36),primary_key=True),sa.Column('delivery_date',sa.String(10),nullable=False),sa.Column('version',sa.Integer,nullable=False),
                    sa.Column('parent_id',sa.String(36),sa.ForeignKey('plan_versions.id')),sa.Column('snapshot_id',sa.String(36),sa.ForeignKey('input_snapshots.id'),nullable=False),
                    sa.Column('job_id',sa.String(36),sa.ForeignKey('planning_jobs.id'),unique=True),sa.Column('status',sa.String(20),nullable=False),
                    sa.Column('content',sa.Text,nullable=False),sa.Column('content_hash',sa.String(64),nullable=False),sa.Column('note',sa.String(240),nullable=False),
                    sa.Column('created_by',sa.String(36),sa.ForeignKey('accounts.id'),nullable=False),sa.Column('created_by_name',sa.String(80),nullable=False),sa.Column('created_at',sa.BigInteger,nullable=False),
                    sa.UniqueConstraint('delivery_date','version',name='uq_plan_version'))
    op.create_index('ix_plan_versions_delivery_date','plan_versions',['delivery_date'])


def downgrade():
    for table in ['plan_versions','planning_jobs','road_cache']: op.drop_table(table)
