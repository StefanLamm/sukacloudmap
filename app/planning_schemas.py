from datetime import date, datetime
from typing import Annotated
from pydantic import Field, StrictInt, field_validator
from app.schemas import Input
from app.master_schemas import BEIJING


class ColumnMapping(Input):
    store_name: Annotated[StrictInt, Field(ge=0, le=99)]
    pieces: Annotated[StrictInt, Field(ge=0, le=99)]
    store_code: Annotated[StrictInt, Field(ge=0, le=99)] | None = None
    delivery_date: Annotated[StrictInt, Field(ge=0, le=99)] | None = None


class CommitDemand(Input):
    delivery_date: date
    preview_revision: Annotated[StrictInt, Field(ge=1)]


class FreezeInput(Input):
    expected_revision: Annotated[StrictInt, Field(ge=1)]
    expected_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')


class StoreDayInput(Input):
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    latest_arrival_at: datetime | None

    @field_validator('latest_arrival_at')
    @classmethod
    def aware(cls,value):
        if value is not None and value.tzinfo is None:
            raise ValueError('最晚到店需要日期和时区')
        return value.astimezone(BEIJING) if value else None


class CreateDemandGroup(Input):
    name: str = Field(min_length=1,max_length=120)
    @field_validator('name')
    @classmethod
    def trim_name(cls,value):
        value=value.strip()
        if not value:raise ValueError('分组名称不能为空')
        return value


class ManualDemandItem(Input):
    store_id: str = Field(min_length=1,max_length=36)
    pieces: Annotated[StrictInt, Field(ge=1,le=1000000000)]


class ManualDemands(Input):
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    items: list[ManualDemandItem] = Field(max_length=10000)
