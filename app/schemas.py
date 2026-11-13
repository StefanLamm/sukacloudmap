import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=128)


class CreateAccount(Input):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=12, max_length=128)
    is_admin: bool = False

    @field_validator("username")
    @classmethod
    def username_format(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", value):
            raise ValueError("账号需为3至64位字母、数字、点、下划线或短横线")
        return value

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value):
        if not value.strip():
            raise ValueError("姓名不能为空")
        return value.strip()


class UpdateAccount(Input):
    expected_revision: int = Field(ge=1)
    active: bool | None = None
    is_admin: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value):
        if value is not None and not value.strip():
            raise ValueError("姓名不能为空")
        return value.strip() if value is not None else value


class ResetPassword(Input):
    expected_revision: int = Field(ge=1)
    password: SecretStr = Field(min_length=12, max_length=128)


class CreateRecord(Input):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=5000)
    task_date: str = Field(default_factory=lambda: datetime.now(timezone(timedelta(hours=8))).date().isoformat())

    @field_validator("task_date")
    @classmethod
    def valid_task_date(cls, value):
        if value is not None and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or date.fromisoformat(value).isoformat() != value):
            raise ValueError("任务日期格式不正确")
        return value

    @field_validator("title", "content")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("内容不能为空")
        return value.strip()


class UpdateRecord(CreateRecord):
    expected_revision: int = Field(ge=1)
    task_date: str | None = None


class CompleteRecord(Input):
    expected_revision: int = Field(ge=1)
    completed: bool = True

