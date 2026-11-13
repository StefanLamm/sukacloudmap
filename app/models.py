import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> int:
    return int(time.time())


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class SystemState(Base):
    __tablename__ = "system_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    master_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    import_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (CheckConstraint("revision > 0", name="ck_account_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(Text)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_revision: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)
    deleted_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_login_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    auth_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[int] = mapped_column(BigInteger)


class SharedRecord(Base):
    __tablename__ = "shared_records"
    __table_args__ = (CheckConstraint("revision > 0", name="ck_record_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_date: Mapped[str] = mapped_column(String(10), default=lambda: datetime.now(timezone(timedelta(hours=8))).date().isoformat())
    completed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completed_by: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    updated_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(String(240))
    occurred_at: Mapped[int] = mapped_column(BigInteger, default=now, index=True)


class Warehouse(Base):
    __tablename__ = "warehouses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(300))
    longitude: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latitude: Mapped[str | None] = mapped_column(String(32), nullable=True)
    crs: Mapped[str] = mapped_column(String(12), default="GCJ02")
    location_source: Mapped[str] = mapped_column(String(80))
    departure_time: Mapped[str] = mapped_column(String(5))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)


class QuoteVersion(Base):
    __tablename__ = "quote_versions"
    __table_args__ = (UniqueConstraint("quote_id", "version", name="uq_quote_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(32))
    per_km: Mapped[str | None] = mapped_column(String(32), nullable=True)
    base_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    included_stops: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_stop_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    region_surcharge: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    signature: Mapped[str] = mapped_column(String(64), index=True)
    effective_from: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))


class Store(Base):
    __tablename__ = "stores"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    province: Mapped[str] = mapped_column(String(80), default="", server_default="")
    district: Mapped[str] = mapped_column(String(80), default="", server_default="")
    region_code: Mapped[str] = mapped_column(String(6), default="", server_default="")
    city: Mapped[str] = mapped_column(String(80))
    address: Mapped[str] = mapped_column(String(300))
    longitude: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latitude: Mapped[str | None] = mapped_column(String(32), nullable=True)
    crs: Mapped[str] = mapped_column(String(12), default="GCJ02")
    location_source: Mapped[str] = mapped_column(String(80))
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"), index=True)
    business_open: Mapped[str] = mapped_column(String(5))
    business_close: Mapped[str] = mapped_column(String(5))
    close_day_offset: Mapped[int] = mapped_column(Integer, default=0)
    unload_minutes: Mapped[int] = mapped_column(Integer)
    latest_arrival_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    dedicated_quote_id: Mapped[str | None] = mapped_column(ForeignKey("quotes.id"), nullable=True)
    common_unit_price: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plate: Mapped[str] = mapped_column(String(16), unique=True)
    max_pieces: Mapped[int] = mapped_column(Integer)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"), index=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cartype: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plate_color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    length_m: Mapped[str | None] = mapped_column(String(32), nullable=True)
    width_m: Mapped[str | None] = mapped_column(String(32), nullable=True)
    height_m: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rated_load_t: Mapped[str | None] = mapped_column(String(32), nullable=True)
    full_gross_t: Mapped[str | None] = mapped_column(String(32), nullable=True)
    axles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)


class WarehouseDay(Base):
    __tablename__ = "warehouse_days"
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    delivery_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    departure_time: Mapped[str] = mapped_column(String(5))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class VehicleDay(Base):
    __tablename__ = "vehicle_days"
    vehicle_id: Mapped[str] = mapped_column(ForeignKey("vehicles.id"), primary_key=True)
    delivery_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"))
    available: Mapped[bool] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class MasterImport(Base):
    __tablename__ = "master_imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(20))
    file_name: Mapped[str] = mapped_column(String(200))
    file_hash: Mapped[str] = mapped_column(String(64))
    master_revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="PREVIEW")
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    committed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class LocationTask(Base):
    __tablename__ = 'location_tasks'
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    query: Mapped[str] = mapped_column(String(400))
    token: Mapped[str] = mapped_column(String(36), default=new_id)
    status: Mapped[str] = mapped_column(String(16), default='QUEUED', index=True)
    reason: Mapped[str] = mapped_column(String(200), default='')
    lease_until: Mapped[int] = mapped_column(BigInteger, default=0)
    last_attempt_at: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class StoreDay(Base):
    __tablename__ = "store_days"
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), primary_key=True)
    delivery_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    latest_arrival_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class DemandImport(Base):
    __tablename__ = "demand_imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("demand_groups.id"), nullable=True, index=True)
    delivery_date: Mapped[str] = mapped_column(String(10), index=True)
    file_name: Mapped[str] = mapped_column(String(200))
    file_hash: Mapped[str] = mapped_column(String(64))
    document: Mapped[str] = mapped_column(Text)
    mapping: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    configuration_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="UPLOADED")
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    previewed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    committed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class DemandDay(Base):
    __tablename__ = "demand_days"
    delivery_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    latest_import_id: Mapped[str] = mapped_column(ForeignKey("demand_imports.id"))
    updated_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)


class DailyDemand(Base):
    __tablename__ = "daily_demands"
    __table_args__ = (UniqueConstraint("delivery_date", "store_id", "group_id", name="uq_daily_store_group"), CheckConstraint("pieces > 0", name="ck_daily_pieces"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("demand_groups.id"), nullable=True, index=True)
    delivery_date: Mapped[str] = mapped_column(ForeignKey("demand_days.delivery_date"), index=True)
    warehouse_id: Mapped[str] = mapped_column(ForeignKey("warehouses.id"))
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"))
    pieces: Mapped[int] = mapped_column(BigInteger)
    source_rows: Mapped[str] = mapped_column(Text)
    import_id: Mapped[str] = mapped_column(ForeignKey("demand_imports.id"))


class InputSnapshot(Base):
    __tablename__ = "input_snapshots"
    __table_args__ = (UniqueConstraint("delivery_date", "version", name="uq_snapshot_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("demand_groups.id"), nullable=True, index=True)
    delivery_date: Mapped[str] = mapped_column(ForeignKey("demand_days.delivery_date"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    demand_revision: Mapped[int] = mapped_column(Integer)
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    created_by_name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class RoadCache(Base):
    __tablename__ = 'road_cache'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[int] = mapped_column(BigInteger, index=True)


class PlanningJob(Base):
    __tablename__ = 'planning_jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("demand_groups.id"), nullable=True, index=True)
    delivery_date: Mapped[str] = mapped_column(String(10), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey('input_snapshots.id'))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20), default='QUEUED', index=True)
    request: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(240), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_until: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    created_by_name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class PlanVersion(Base):
    __tablename__ = 'plan_versions'
    __table_args__ = (UniqueConstraint('delivery_date', 'version', name='uq_plan_version'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    group_id: Mapped[str | None] = mapped_column(ForeignKey("demand_groups.id"), nullable=True, index=True)
    delivery_date: Mapped[str] = mapped_column(String(10), index=True)
    version: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey('plan_versions.id'), nullable=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey('input_snapshots.id'))
    job_id: Mapped[str | None] = mapped_column(ForeignKey('planning_jobs.id'), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(20), default='DRAFT')
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(String(240), default='')
    created_by: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    created_by_name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class DemandGroup(Base):
    __tablename__ = 'demand_groups'
    __table_args__ = (UniqueConstraint('delivery_date', 'name', name='uq_demand_group_name'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    delivery_date: Mapped[str] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(String(120))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    latest_import_id: Mapped[str | None] = mapped_column(ForeignKey('demand_imports.id',name='fk_demand_groups_latest_import',use_alter=True), nullable=True)
    updated_by: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now)


class GroupArrangement(Base):
    __tablename__ = 'group_arrangements'
    group_id: Mapped[str] = mapped_column(ForeignKey('demand_groups.id'), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
