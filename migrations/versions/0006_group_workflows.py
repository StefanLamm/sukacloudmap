"""Demand groups, region pricing and dated shared tasks; legacy history retained."""
from alembic import op
import sqlalchemy as sa
import uuid
import json

revision = '0006_group_workflows'
down_revision = '0005_location_tasks'
branch_labels = depends_on = None


def add_reference(table, name, target):
    # SQLite supports nullable REFERENCES in ADD COLUMN directly; no parent
    # table rebuild (and no disabling foreign-key enforcement) is needed.
    if op.get_bind().dialect.name == 'sqlite':
        op.execute(sa.text(f'ALTER TABLE {table} ADD COLUMN {name} VARCHAR(36) REFERENCES {target}(id)'))
    else:
        op.add_column(table, sa.Column(name, sa.String(36), sa.ForeignKey(target+'.id'), nullable=True))


def upgrade():
    op.add_column('accounts', sa.Column('deleted_at', sa.BigInteger(), nullable=True))
    op.add_column('shared_records', sa.Column('task_date', sa.String(10), nullable=False, server_default='1970-01-01'))
    op.add_column('shared_records', sa.Column('completed_at', sa.BigInteger(), nullable=True))
    add_reference('shared_records','completed_by','accounts')
    if op.get_bind().dialect.name == 'sqlite':
        op.execute("UPDATE shared_records SET task_date=date(created_at,'unixepoch','+8 hours')")
    else:
        op.execute("UPDATE shared_records SET task_date=to_char(to_timestamp(created_at) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD')")
    for name in ['province','district']:
        op.add_column('stores',sa.Column(name,sa.String(80),nullable=False,server_default=''))
    op.add_column('stores',sa.Column('region_code',sa.String(6),nullable=False,server_default=''))
    op.add_column('quote_versions',sa.Column('region_surcharge',sa.Text(),nullable=False,server_default='{}'))
    with op.batch_alter_table('quote_versions') as batch:
        batch.alter_column('mode',existing_type=sa.String(16),type_=sa.String(32),existing_nullable=False)
    op.create_table('demand_groups',sa.Column('id',sa.String(36),primary_key=True),sa.Column('delivery_date',sa.String(10),nullable=False),
        sa.Column('name',sa.String(120),nullable=False),sa.Column('revision',sa.Integer,nullable=False),
        sa.Column('latest_import_id',sa.String(36),sa.ForeignKey('demand_imports.id',name='fk_demand_groups_latest_import'),nullable=True),
        sa.Column('updated_by',sa.String(36),sa.ForeignKey('accounts.id'),nullable=False),
        sa.Column('created_at',sa.BigInteger,nullable=False),sa.Column('updated_at',sa.BigInteger,nullable=False),
        sa.UniqueConstraint('delivery_date','name',name='uq_demand_group_name'))
    op.create_index('ix_demand_groups_delivery_date','demand_groups',['delivery_date'])
    for table in ['demand_imports','daily_demands','input_snapshots','planning_jobs','plan_versions']:
        add_reference(table,'group_id','demand_groups')
        op.create_index('ix_'+table+'_group_id',table,['group_id'])
    with op.batch_alter_table('daily_demands') as batch:
        batch.drop_constraint('uq_daily_store',type_='unique')
        batch.create_unique_constraint('uq_daily_store_group',['delivery_date','store_id','group_id'])
    op.create_table('group_arrangements',sa.Column('group_id',sa.String(36),sa.ForeignKey('demand_groups.id'),primary_key=True),
        sa.Column('kind',sa.String(20),primary_key=True),sa.Column('entity_id',sa.String(36),primary_key=True),
        sa.Column('payload',sa.Text,nullable=False),sa.Column('revision',sa.Integer,nullable=False))

    bind=op.get_bind()
    # Group existing business data without rewriting immutable JSON/history.
    for state in list(bind.execute(sa.text('SELECT * FROM demand_days')).mappings()):
        gid=str(uuid.uuid4())
        bind.execute(sa.text('INSERT INTO demand_groups (id,delivery_date,name,revision,latest_import_id,updated_by,created_at,updated_at) VALUES (:id,:day,:name,:revision,:batch,:actor,:created,:updated)'),
            dict(id=gid,day=state['delivery_date'],name='历史需求',revision=state['revision'],batch=state['latest_import_id'],actor=state['updated_by'],created=state['updated_at'],updated=state['updated_at']))
        for table in ['demand_imports','daily_demands','input_snapshots','planning_jobs','plan_versions']:
            bind.execute(sa.text(f'UPDATE {table} SET group_id=:gid WHERE delivery_date=:day'),dict(gid=gid,day=state['delivery_date']))
        for table,kind,key,fields in [('warehouse_days','warehouses','warehouse_id',['departure_time']),('vehicle_days','vehicles','vehicle_id',['warehouse_id','available']),('store_days','stores','store_id',['latest_arrival_at'])]:
            for row in bind.execute(sa.text(f'SELECT * FROM {table} WHERE delivery_date=:day'),dict(day=state['delivery_date'])).mappings():
                values={f:(bool(row[f]) if f=='available' else row[f]) for f in fields}
                bind.execute(sa.text('INSERT INTO group_arrangements (group_id,kind,entity_id,payload,revision) VALUES (:gid,:kind,:entity,:payload,:revision)'),dict(gid=gid,kind=kind,entity=row[key],payload=json.dumps(values,ensure_ascii=False),revision=row['revision']))


def downgrade():
    bind=op.get_bind()
    protected=[
        'SELECT count(*) FROM demand_groups',
        'SELECT count(*) FROM accounts WHERE deleted_at IS NOT NULL',
        "SELECT count(*) FROM quote_versions WHERE mode NOT IN ('KM','BASE_STOPS') OR region_surcharge NOT IN ('{}','')",
        "SELECT count(*) FROM stores WHERE province<>'' OR district<>'' OR region_code<>''",
        'SELECT count(*) FROM shared_records WHERE completed_at IS NOT NULL OR completed_by IS NOT NULL',
    ]
    if any(bind.scalar(sa.text(query)) for query in protected):
        raise RuntimeError('分组、区域报价或任务状态已使用，不能无损降级；请使用升级前数据库备份恢复。')
    # Even an unfinished task may have a manually selected future/past date.
    date_expression="date(created_at,'unixepoch','+8 hours')" if bind.dialect.name=='sqlite' else "to_char(to_timestamp(created_at) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD')"
    if bind.scalar(sa.text(f'SELECT count(*) FROM shared_records WHERE task_date<>{date_expression}')):
        raise RuntimeError('共享任务日期已使用，不能无损降级；请使用升级前数据库备份恢复。')
    op.drop_table('group_arrangements')
    op.drop_index('ix_daily_demands_group_id',table_name='daily_demands')
    with op.batch_alter_table('daily_demands') as batch:
        batch.drop_constraint('uq_daily_store_group',type_='unique')
        batch.create_unique_constraint('uq_daily_store',['delivery_date','store_id'])
        # Its group FK was promoted to a table constraint by the upgrade's
        # batch rebuild, so SQLite needs the column removed in this rebuild.
        batch.drop_column('group_id')
    for table in ['plan_versions','planning_jobs','input_snapshots','demand_imports']:
        op.drop_index('ix_'+table+'_group_id',table_name=table)
        op.drop_column(table,'group_id')
    op.drop_table('demand_groups')
    for name in ['completed_by','completed_at','task_date']:op.drop_column('shared_records',name)
    op.drop_column('accounts','deleted_at')
    op.drop_column('quote_versions','region_surcharge')
    with op.batch_alter_table('quote_versions') as batch:
        batch.alter_column('mode',existing_type=sa.String(32),type_=sa.String(16),existing_nullable=False)
    for name in ['region_code','district','province']:op.drop_column('stores',name)
