from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.resource import ClusterFilter


class ModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    cluster: Literal["yz", "zw"]
    namespace_name: str = Field(min_length=1, max_length=256)
    pod_name: str = Field(min_length=1, max_length=256)
    container_name: str = Field(min_length=1, max_length=256)
    model_key: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)


class StatisticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster: ClusterFilter = "all"


class DeviceHistoryRequest(StatisticsRequest):
    window: Literal["1h", "1d", "1w", "1mo", "3mo"] = "1h"
    node: str = Field(min_length=1, max_length=256)
    device_uuid: str = Field(min_length=1, max_length=256)
