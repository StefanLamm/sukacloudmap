"""仓库、门店、车辆、不可变报价版本与资料导入。"""
from alembic import op
import sqlalchemy as sa

revision = "0002_master_data"
down_revision = "0001_accounts"
branch_labels = depends_on = None


def common():
    return [sa.Column("revision", sa.Integer, nullable=False), sa.Column("created_at", sa.BigInteger, nullable=False), sa.Column("updated_at", sa.BigInteger, nullable=False)]


def location():
    return [sa.Column("address", sa.String(300), nullable=False), sa.Column("longitude", sa.String(32)), sa.Column("latitude", sa.String(32)),
            sa.Column("crs", sa.String(12), nullable=False), sa.Column("location_source", sa.String(80), nullable=False)]


def upgrade():
    op.add_column("system_state", sa.Column("master_revision", sa.Integer, nullable=False, server_default="1"))
    op.create_table("warehouses", sa.Column("id", sa.String(36), primary_key=True), sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False), *location(), sa.Column("departure_time", sa.String(5), nullable=False), *common())
    op.create_table("quotes", sa.Column("id", sa.String(36), primary_key=True), sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False), *common())
    op.create_table("quote_versions", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("quote_id", sa.String(36), sa.ForeignKey("quotes.id"), nullable=False), sa.Column("version", sa.Integer, nullable=False),
        sa.Column("name", sa.String(120), nullable=False), sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("per_km", sa.String(32)), sa.Column("base_price", sa.String(32)), sa.Column("included_stops", sa.Integer), sa.Column("extra_stop_price", sa.String(32)),
        sa.Column("signature", sa.String(64), nullable=False), sa.Column("effective_from", sa.String(10), nullable=False),
        sa.Column("created_at", sa.BigInteger, nullable=False), sa.Column("created_by", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.UniqueConstraint("quote_id", "version", name="uq_quote_version"))
    op.create_index("ix_quote_versions_quote_id", "quote_versions", ["quote_id"])
    op.create_index("ix_quote_versions_signature", "quote_versions", ["signature"])
    op.create_table("stores", sa.Column("id", sa.String(36), primary_key=True), sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False), sa.Column("city", sa.String(80), nullable=False), *location(),
        sa.Column("warehouse_id", sa.String(36), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("business_open", sa.String(5), nullable=False), sa.Column("business_close", sa.String(5), nullable=False),
        sa.Column("close_day_offset", sa.Integer, nullable=False), sa.Column("unload_minutes", sa.Integer, nullable=False),
        sa.Column("latest_arrival_at", sa.String(40)), sa.Column("dedicated_quote_id", sa.String(36), sa.ForeignKey("quotes.id")), sa.Column("common_unit_price", sa.String(32)), *common())
    op.create_index("ix_stores_name", "stores", ["name"])
    op.create_index("ix_stores_warehouse_id", "stores", ["warehouse_id"])
    op.create_table("vehicles", sa.Column("id", sa.String(36), primary_key=True), sa.Column("plate", sa.String(16), nullable=False, unique=True),
        sa.Column("max_pieces", sa.Integer, nullable=False), sa.Column("warehouse_id", sa.String(36), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("available", sa.Boolean, nullable=False), sa.Column("size", sa.Integer), sa.Column("cartype", sa.Integer), sa.Column("plate_color", sa.String(20)),
        *[sa.Column(n, sa.String(32)) for n in ["length_m", "width_m", "height_m", "rated_load_t", "full_gross_t"]], sa.Column("axles", sa.Integer), *common())
    op.create_index("ix_vehicles_warehouse_id", "vehicles", ["warehouse_id"])
    op.create_table("warehouse_days", sa.Column("warehouse_id", sa.String(36), sa.ForeignKey("warehouses.id"), primary_key=True),
        sa.Column("delivery_date", sa.String(10), primary_key=True), sa.Column("departure_time", sa.String(5), nullable=False), sa.Column("revision", sa.Integer, nullable=False))
    op.create_table("vehicle_days", sa.Column("vehicle_id", sa.String(36), sa.ForeignKey("vehicles.id"), primary_key=True),
        sa.Column("delivery_date", sa.String(10), primary_key=True), sa.Column("warehouse_id", sa.String(36), sa.ForeignKey("warehouses.id"), nullable=False),
        sa.Column("available", sa.Boolean, nullable=False), sa.Column("revision", sa.Integer, nullable=False))
    op.create_table("master_imports", sa.Column("id", sa.String(36), primary_key=True), sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("file_name", sa.String(200), nullable=False), sa.Column("file_hash", sa.String(64), nullable=False), sa.Column("master_revision", sa.Integer, nullable=False),
        sa.Column("payload", sa.Text, nullable=False), sa.Column("result", sa.Text, nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False), sa.Column("created_at", sa.BigInteger, nullable=False), sa.Column("committed_at", sa.BigInteger))


def downgrade():
    for table in ["master_imports", "vehicle_days", "warehouse_days", "vehicles", "stores", "quote_versions", "quotes", "warehouses"]:
        op.drop_table(table)
    op.drop_column("system_state", "master_revision")
