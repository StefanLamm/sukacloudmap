from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUKA_", env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True)

    environment: Literal["development", "production", "test"] = "development"
    database_url: SecretStr = SecretStr(f"sqlite:///{ROOT / 'data' / 'suka.db'}")
    public_origin: str = "http://127.0.0.1:8000"
    allowed_hosts: list[str] = ["127.0.0.1", "localhost"]
    secure_cookie: bool = False
    session_hours: int = Field(default=12, ge=1, le=168)
    login_max_attempts: int = Field(default=10, ge=3, le=100)
    amap_js_key: SecretStr | None = None
    amap_security_js_code: SecretStr | None = None
    amap_web_service_key: SecretStr | None = None
    amap_rate_limit_path: Path = ROOT / 'data' / 'amap' / 'rate-limit.db'
    amap_reference_graph_path: Path = ROOT / 'data' / 'amap' / 'reference-graph.db'
    amap_reference_graph_test_enabled: bool = False
    amap_web_qps: int = Field(default=3, ge=1, le=3)
    amap_js_qps: int = Field(default=10, ge=1, le=10)
    truck_live_calls_enabled: bool = False
    truck_job_max_calls: int | None = Field(default=None, ge=0)
    planning_route_mode: Literal['TRUCK','DRIVING'] = 'TRUCK'
    planning_optimizer_profile: Literal['HYBRID','ALNS','V3_REFERENCE','GUIDED_LOCAL_SEARCH','SIMULATED_ANNEALING','TABU_SEARCH'] = 'HYBRID'
    planning_common_penalty_multiplier: float = Field(default=1,ge=.1,le=10,allow_inf_nan=False)
    driving_live_calls_enabled: bool = False
    driving_job_max_calls: int | None = Field(default=None, ge=0)
    planning_road_source: Literal['LIVE','TEST_GRID'] = 'LIVE'
    cookie_namespace: str = Field(default='',pattern=r'^[a-z0-9_]{0,24}$')
    uat_delivery_date: str | None = Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}$')
    runtime_label: str = Field(default='', max_length=100)
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_provider: Literal['AMAP', 'OPEN'] = 'AMAP'
    map_geocoder_url: str = "https://photon.komoot.io/api/"
    map_auto_geocode_enabled: bool = True

    @field_validator('truck_job_max_calls', 'driving_job_max_calls', mode='before')
    @classmethod
    def unlimited_calls(cls, value):
        return None if isinstance(value, str) and value.strip().lower() in {'unlimited','none','null',''} else value

    @model_validator(mode="after")
    def production_settings(self):
        if self.planning_road_source == 'TEST_GRID' and (self.environment != 'test' or self.truck_live_calls_enabled or self.driving_live_calls_enabled):
            raise ValueError('模拟道路仅允许隔离test环境，且必须关闭真实道路调用')
        for value in [self.map_tile_url, self.map_geocoder_url]:
            url = urlsplit(value)
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError('地图服务必须使用不含凭证的HTTPS地址')
        if self.environment == "production":
            if not self.secure_cookie or not self.public_origin.startswith("https://"):
                raise ValueError("正式环境必须配置 HTTPS PUBLIC_ORIGIN 和 SECURE_COOKIE=true")
            if "*" in self.allowed_hosts:
                raise ValueError("正式环境 ALLOWED_HOSTS 不能包含通配符")
        return self

    @property
    def road_calls_enabled(self) -> bool:
        return self.driving_live_calls_enabled if self.planning_route_mode == 'DRIVING' else self.truck_live_calls_enabled

    @property
    def road_job_max_calls(self) -> int | None:
        return self.driving_job_max_calls if self.planning_route_mode == 'DRIVING' else self.truck_job_max_calls

    @property
    def cookie_name(self) -> str:
        name='suka_'+(self.cookie_namespace+'_' if self.cookie_namespace else '')+'session'
        return '__Host-'+name if self.secure_cookie else name

