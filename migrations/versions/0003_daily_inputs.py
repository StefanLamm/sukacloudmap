"""每日需求、来源批次、当日收货限制与不可变规划输入。"""
from alembic import op
import sqlalchemy as sa

revision = "0003_daily_inputs"
down_revision = "0002_master_data"
branch_labels = depends_on = None


def upgrade():
    op.create_table('store_days', sa.Column('store_id',sa.String(36),sa.ForeignKey('stores.id'),primary_key=True),
                    sa.Column('delivery_date',sa.String(10),primary_key=True),sa.Column('latest_arrival_at',sa.String(40)),sa.Column('revision',sa.Integer,nullable=False))
    op.create_table('demand_imports',sa.Column('id',sa.String(36),primary_key=True),sa.Column('delivery_date',sa.String(10),nullable=False),
                    sa.Column('file_name',sa.String(200),nullable=False),sa.Column('file_hash',sa.String(64),nullable=False),sa.Column('document',sa.Text,nullable=False),
                    *[sa.Column(k,sa.Text) for k in ['mapping','payload','result']],sa.Column('configuration_hash',sa.String(64)),sa.Column('expected_revision',sa.Integer),
                    sa.Column('preview_revision',sa.Integer,nullable=False),sa.Column('status',sa.String(20),nullable=False),
                    sa.Column('created_by',sa.String(36),sa.ForeignKey('accounts.id'),nullable=False),sa.Column('created_at',sa.BigInteger,nullable=False),
                    sa.Column('previewed_at',sa.BigInteger),sa.Column('committed_at',sa.BigInteger))
    op.create_index('ix_demand_imports_delivery_date','demand_imports',['delivery_date'])
    op.create_table('demand_days',sa.Column('delivery_date',sa.String(10),primary_key=True),sa.Column('revision',sa.Integer,nullable=False),
                    sa.Column('latest_import_id',sa.String(36),sa.ForeignKey('demand_imports.id'),nullable=False),
                    sa.Column('updated_by',sa.String(36),sa.ForeignKey('accounts.id'),nullable=False),sa.Column('updated_at',sa.BigInteger,nullable=False))
    op.create_table('daily_demands',sa.Column('id',sa.String(36),primary_key=True),
                    sa.Column('delivery_date',sa.String(10),sa.ForeignKey('demand_days.delivery_date'),nullable=False),
                    sa.Column('warehouse_id',sa.String(36),sa.ForeignKey('warehouses.id'),nullable=False),
                    sa.Column('store_id',sa.String(36),sa.ForeignKey('stores.id'),nullable=False),sa.Column('pieces',sa.BigInteger,nullable=False),
                    sa.Column('source_rows',sa.Text,nullable=False),sa.Column('import_id',sa.String(36),sa.ForeignKey('demand_imports.id'),nullable=False),
                    sa.UniqueConstraint('delivery_date','store_id',name='uq_daily_store'),sa.CheckConstraint('pieces > 0',name='ck_daily_pieces'))
    op.create_index('ix_daily_demands_delivery_date','daily_demands',['delivery_date'])
    op.create_table('input_snapshots',sa.Column('id',sa.String(36),primary_key=True),
                    sa.Column('delivery_date',sa.String(10),sa.ForeignKey('demand_days.delivery_date'),nullable=False),sa.Column('version',sa.Integer,nullable=False),
                    sa.Column('demand_revision',sa.Integer,nullable=False),sa.Column('input_fingerprint',sa.String(64),nullable=False),
                    sa.Column('content_hash',sa.String(64),nullable=False),sa.Column('content',sa.Text,nullable=False),
                    sa.Column('created_by',sa.String(36),sa.ForeignKey('accounts.id'),nullable=False),sa.Column('created_by_name',sa.String(80),nullable=False),
                    sa.Column('created_at',sa.BigInteger,nullable=False),sa.UniqueConstraint('delivery_date','version',name='uq_snapshot_version'))
    op.create_index('ix_input_snapshots_delivery_date','input_snapshots',['delivery_date'])


def downgrade():
    for table in ['input_snapshots','daily_demands','demand_days','demand_imports','store_days']:
        op.drop_table(table)
