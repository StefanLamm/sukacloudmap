"""基础资料输入约束；金额与道路参数以十进制文本持久保存。"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
import re

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator
from app.schemas import Input

Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=6)]
PositiveDecimal = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=6)]
Clock = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
BEIJING = timezone(timedelta(hours=8))


def today() -> date:
    return datetime.now(BEIJING).date()


def decimal_text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def plain(value):
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    return value


class MasterInput(Input):
    @field_validator("*", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("longitude", "latitude", "latest_arrival_at", "dedicated_quote_id", "common_unit_price", "per_km", "base_price", "included_stops", "extra_stop_price", "size", "cartype", "plate_color", "length_m", "width_m", "height_m", "rated_load_t", "full_gross_t", "axles", mode="before", check_fields=False)
    @classmethod
    def empty_optional(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


class MasterSelection(Input):
    id: str = Field(min_length=1, max_length=36)
    expected_revision: Annotated[StrictInt, Field(ge=1)]


class MasterBatchSelection(Input):
    items: list[MasterSelection] = Field(min_length=1, max_length=10000)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError('不能重复选择同一条资料')
        return self


class Coded(MasterInput):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", value):
            raise ValueError("编号需为1至40位字母、数字、点、下划线或短横线")
        return value.upper()


class Location(Coded):
    address: str = Field(min_length=1, max_length=300)
    longitude: Annotated[Decimal, Field(ge=-180, le=180, max_digits=12, decimal_places=8)] | None = None
    latitude: Annotated[Decimal, Field(ge=-90, le=90, max_digits=12, decimal_places=8)] | None = None
    crs: Literal["GCJ02", "WGS84", "BD09"] = "GCJ02"
    location_source: str = Field(default="手工录入", min_length=1, max_length=80)

    @model_validator(mode="after")
    def coordinate_pair(self):
        if (self.longitude is None) != (self.latitude is None):
            raise ValueError("经度和纬度必须同时填写或同时留空")
        if self.longitude is not None and "crs" not in self.model_fields_set:
            raise ValueError("填写经纬度时必须明确坐标系，不自动猜测")
        return self


class WarehouseInput(Location):
    departure_time: Clock


class StoreInput(Location):
    province: str = Field(default="", max_length=80)
    city: str = Field(default="", max_length=80)
    district: str = Field(default="", max_length=80)
    region_code: str = Field(default="", pattern=r"^(?:\d{6})?$")
    warehouse_id: str = Field(min_length=1, max_length=36)
    business_open: Clock
    business_close: Clock
    close_day_offset: Annotated[StrictInt, Field(ge=0, le=1)] = 0
    unload_minutes: Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
    latest_arrival_at: datetime | None = None
    dedicated_quote_id: str | None = Field(default=None, max_length=36)
    common_unit_price: Money | None = None

    @model_validator(mode="after")
    def operating_hours(self):
        if self.close_day_offset == 0 and self.business_close <= self.business_open:
            raise ValueError("同日闭店时间必须晚于开门时间；跨日营业请明确选择次日闭店")
        if self.latest_arrival_at is not None:
            if self.latest_arrival_at.tzinfo is None:
                raise ValueError("最晚到店需包含日期和时区，例如2026-11-11T09:00:00+08:00")
            self.latest_arrival_at = self.latest_arrival_at.astimezone(BEIJING)
        return self


class VehicleInput(MasterInput):
    plate: str = Field(min_length=7, max_length=16)
    max_pieces: Annotated[StrictInt, Field(gt=0, le=1_000_000_000)]
    warehouse_id: str = Field(min_length=1, max_length=36)
    available: StrictBool = True
    size: Annotated[StrictInt, Field(ge=1, le=4)] | None = None
    cartype: Annotated[StrictInt, Field(ge=0, le=2)] | None = None
    plate_color: Literal["BLUE", "YELLOW", "GREEN", "YELLOW_GREEN", "WHITE", "BLACK", "OTHER"] | None = None
    length_m: PositiveDecimal | None = None
    width_m: PositiveDecimal | None = None
    height_m: PositiveDecimal | None = None
    rated_load_t: PositiveDecimal | None = None
    full_gross_t: PositiveDecimal | None = None
    axles: Annotated[StrictInt, Field(ge=1, le=255)] | None = None

    @field_validator("plate")
    @classmethod
    def normalize_plate(cls, value):
        value = re.sub(r"[\s·•]", "", value).upper()
        provinces = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼"
        if not re.fullmatch(f"[{provinces}][A-Z](?:[A-Z0-9]{{5,6}}|[A-Z0-9]{{4}}[挂学警港澳])", value):
            raise ValueError("请填写完整车牌，例如苏A12345或苏AD12345")
        return value

    @model_validator(mode="after")
    def weights(self):
        if self.rated_load_t is not None and self.full_gross_t is not None and self.full_gross_t < self.rated_load_t:
            raise ValueError("满载总重不能小于核定载重")
        return self


class QuoteInput(Coded):
    mode: Literal["KM", "BASE_STOPS", "KM_REGION", "BASE_STOPS_REGION"]
    per_km: Money | None = None
    base_price: Money | None = None
    included_stops: Annotated[StrictInt, Field(ge=0, le=1_000_000_000)] | None = None
    extra_stop_price: Money | None = None
    effective_from: date = Field(default_factory=today)
    region_surcharge: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def parameters(self):
        if self.mode in {"KM", "KM_REGION"}:
            if self.per_km is None:
                raise ValueError("按公里报价必须填写每公里单价，0元有效")
            if any(v is not None for v in [self.base_price, self.included_stops, self.extra_stop_price]):
                raise ValueError("按公里报价不能包含起送或串店参数")
        else:
            if any(v is None for v in [self.base_price, self.included_stops, self.extra_stop_price]):
                raise ValueError("起送加串店必须填写起送价、包含门店数和串店费，0值有效")
            if self.per_km is not None:
                raise ValueError("起送加串店不能包含公里单价")
        if self.mode.endswith('_REGION'):
            rule = RegionSurcharge.model_validate(self.region_surcharge)
            self.region_surcharge = plain(rule.model_dump())
            # Keep the original once-per-line shape/signature for old quotes.
            if rule.billing_mode == "ONCE":self.region_surcharge.pop("billing_mode", None)
        elif self.region_surcharge:
            raise ValueError('普通报价不能包含特殊地区附加费，请选择相应计费模式')
        return self


class RegionTarget(Input):
    province_code: str = Field(pattern=r'^\d{6}$')
    province_name: str = Field(min_length=1,max_length=80)
    city_code: str = Field(pattern=r'^\d{6}$')
    city_name: str = Field(min_length=1,max_length=80)
    district_code: str = Field(default='',pattern=r'^(?:\d{6})?$')
    district_name: str = Field(default='',max_length=80)

    @model_validator(mode='after')
    def hierarchy(self):
        if self.city_code[:2] != self.province_code[:2] or (self.district_code and self.district_code[:2] != self.province_code[:2]):
            raise ValueError('所选省、市、区县不属于同一行政区')
        if self.district_code and not self.city_code.endswith('0000') and self.district_code[:4] != self.city_code[:4]:raise ValueError('区县不属于所选城市')
        if bool(self.district_code) != bool(self.district_name):raise ValueError('区县名称与编码需同时填写')
        return self


class RegionSurcharge(Input):
    billing_mode: Literal["ONCE", "PER_REGION", "PER_STORE"] = "ONCE"
    amount: Money
    targets: list[RegionTarget] = Field(min_length=1,max_length=300)

    @model_validator(mode='after')
    def unique(self):
        keys=[(r.province_code,r.city_code,r.district_code) for r in self.targets]
        if len(set(keys))!=len(keys):raise ValueError('特殊地区不能重复选择')
        self.targets.sort(key=lambda r:(r.province_code,r.city_code,r.district_code))
        return self


class StoreBulkUpdate(MasterBatchSelection):
    changes: dict = Field(min_length=1)

    @field_validator('changes')
    @classmethod
    def allowed_changes(cls,value):
        allowed={'warehouse_id','business_open','business_close','close_day_offset','unload_minutes','dedicated_quote_id','common_unit_price'}
        if set(value)-allowed:raise ValueError('批量修改包含不支持的字段')
        return value


class WarehouseDayInput(Input):
    departure_time: Clock
    expected_revision: Annotated[StrictInt, Field(ge=0)]


class VehicleDayInput(Input):
    warehouse_id: str = Field(min_length=1, max_length=36)
    available: StrictBool
    expected_revision: Annotated[StrictInt, Field(ge=0)]


class CompatibilityInput(Input):
    store_ids: list[str] = Field(min_length=1, max_length=10000)
    delivery_date: date = Field(default_factory=today)

    @field_validator("store_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("不能重复选择同一门店")
        return value


SCHEMAS = {"warehouses": WarehouseInput, "stores": StoreInput, "vehicles": VehicleInput, "quotes": QuoteInput}
