import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.resources.catalog import BusinessCatalog, load_catalog
from app.resources.models import ModelBinding

CLUSTERS = {"yz": ("亦庄", "k8s_yz_gpu_maas"), "zw": ("兆维", "k8s_zw_gpu_maas")}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    production_mode: bool = False
    production_public_origin: str = ""
    gateway_shared_secret: SecretStr = SecretStr("")
    data_mode: Literal["mock", "live"] = "mock"
    prom_yz_url: str = ""
    prom_zw_url: str = ""
    prom_timeout_seconds: float = Field(default=8, ge=0.1, le=30)
    prom_max_response_bytes: int = Field(default=4_000_000, ge=1024, le=10_000_000)
    prom_max_series: int = Field(default=10000, ge=1, le=20000)
    prom_cache_ttl_seconds: int = Field(default=15, ge=0, le=60)
    prom_stale_retention_seconds: int = Field(default=300, ge=0, le=600)
    range_max_points: int = Field(default=721, ge=61, le=1000)
    range_cache_ttl_seconds: int = Field(default=30, ge=0, le=60)
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "test"]
    stale_after_seconds: int = Field(default=120, ge=10, le=600)
    allocation_absence_is_zero: bool = False
    memory_capacity_unit: Literal["unverified", "MB", "MiB", "bytes"] = "unverified"
    memory_allocation_unit: Literal["unverified", "MB", "MiB", "bytes"] = "unverified"
    mock_scenario: Literal["normal", "partial", "empty", "missing", "overallocated", "stale"] = "normal"
    history_database_url: SecretStr = SecretStr("")
    snapshot_sync_interval_seconds: int = Field(default=30, ge=5, le=300)
    snapshot_refresh_min_interval_seconds: int = Field(default=5, ge=0, le=60)
    history_fill_cooldown_seconds: int = Field(default=30, ge=0, le=300)
    api_requests_per_minute: int = Field(default=60, ge=10, le=600)
    api_request_burst: int = Field(default=30, ge=5, le=100)
    history_sync_enabled: bool = False
    history_sync_interval_seconds: int = Field(default=60, ge=30, le=3600)
    history_chunk_points: int = Field(default=30, ge=1, le=60)
    history_query_fill_chunks: int = Field(default=2, ge=0, le=8)
    resource_backfill_hours_per_tick: int = Field(default=6, ge=1, le=12)
    resource_model_mappings: list[ModelBinding] | None = Field(default=None, max_length=1000)
    resource_model_catalog_file: str = ""
    resource_model_catalog: BusinessCatalog = Field(default_factory=BusinessCatalog, exclude=True)
    analytics_enabled: bool = True
    operations_metrics_enabled: bool = False
    operations_gpu_temperature_c: float = Field(default=85, ge=50, le=110)
    host_metrics_enabled: bool = False
    workload_metrics_enabled: bool = False
    operations_workspace_enabled: bool = False
    investigation_feedback_enabled: bool = False
    efficiency_enabled: bool = False
    efficiency_verified_device_models: list[str] = []
    llm_enabled: bool = False
    llm_data_processing_authorized: bool = False
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_allowed_origins: list[str] = ["https://api.deepseek.com"]
    deepseek_model: str = Field(default="deepseek-flash", min_length=1, max_length=100)
    deepseek_timeout_seconds: int = Field(default=20, ge=2, le=40)
    insight_timeout_seconds: int = Field(default=90, ge=10, le=120)
    insight_max_tool_calls: int = Field(default=24, ge=8, le=32)
    insight_max_model_rounds: int = Field(default=6, ge=1, le=8)
    insight_allowed_clusters: list[Literal["yz", "zw"]] = ["yz", "zw"]

    @model_validator(mode="after")
    def validate_endpoints(self):
        if self.production_mode:
            public = urlsplit(self.production_public_origin)
            if (
                public.scheme != "https"
                or not public.hostname
                or public.hostname not in self.allowed_hosts
                or public.username
                or public.password
                or public.query
                or public.fragment
                or public.path not in ("", "/")
            ):
                raise ValueError("Production requires an approved HTTPS public origin")
            if public.port is not None and not 1 <= public.port <= 65535:
                raise ValueError("Production public origin port is invalid")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.gateway_shared_secret.get_secret_value()):
                raise ValueError("Production requires a 32–128 character URL-safe gateway shared secret")
            if not self.history_database_url.get_secret_value():
                raise ValueError("Production requires PostgreSQL")
            if not self.allowed_hosts or any("*" in h or h == "test" for h in self.allowed_hosts):
                raise ValueError("Production requires explicit approved hosts without wildcards/test")
            if self.operations_workspace_enabled or self.investigation_feedback_enabled:
                raise ValueError("Production human writes require enterprise identity and are not enabled")
            if self.history_query_fill_chunks:
                raise ValueError("Production history reads must disable query-time filling")
            if set(self.insight_allowed_clusters) != set(CLUSTERS):
                raise ValueError(
                    "Production currently supports one approved group for both clusters, not per-user scopes"
                )
        if self.resource_model_catalog_file:
            self.resource_model_catalog = load_catalog(self.resource_model_catalog_file)
        bindings = self.resource_model_mappings or []
        identities = [(b.cluster, b.namespace_name, b.pod_name, b.container_name) for b in bindings]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate resource model binding")
        model_names: dict[str, str] = {}
        for binding in bindings:
            if binding.model_key in model_names and model_names[binding.model_key] != binding.display_name:
                raise ValueError("Conflicting business model display names")
            model_names[binding.model_key] = binding.display_name

        if (
            self.memory_capacity_unit != "unverified"
            and self.memory_allocation_unit != "unverified"
            and self.memory_capacity_unit != self.memory_allocation_unit
        ):
            raise ValueError("Verified capacity and allocation units must match for raw arithmetic")
        if (
            self.history_sync_enabled
            or self.efficiency_enabled
            or self.operations_metrics_enabled
            or self.host_metrics_enabled
            or self.workload_metrics_enabled
        ) and not self.history_database_url.get_secret_value():
            raise ValueError("History synchronization requires PostgreSQL configuration")
        parts = urlsplit(self.deepseek_base_url)
        if (
            self.deepseek_base_url.rstrip("/") not in self.deepseek_allowed_origins
            or parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/", "/v1")
        ):
            raise ValueError("DeepSeek endpoint must be an explicitly approved HTTPS origin")
        if self.llm_enabled and not self.llm_data_processing_authorized:
            raise ValueError("LLM data processing authorization is required")
        if self.data_mode == "live":
            for url in (self.prom_yz_url, self.prom_zw_url):
                parts = urlsplit(url)
                if (
                    parts.scheme not in ("https", "http")
                    or not parts.hostname
                    or parts.username
                    or parts.password
                    or parts.query
                    or parts.fragment
                    or parts.path not in ("", "/")
                ):
                    raise ValueError("Live endpoints must be configured HTTP(S) origins without credentials")
        return self


@lru_cache
def get_settings():
    return Settings()
