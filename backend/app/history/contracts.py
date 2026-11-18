"""Closed history scopes, shared by storage and analysis. No arbitrary SQL expressions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.clients.prometheus import SourceError
from app.core.config import CLUSTERS

Dimension = Literal["cluster", "node", "gpu", "namespace", "pod", "workload"]
ARITY = {"cluster": 0, "node": 1, "gpu": 2, "namespace": 1, "pod": 2, "workload": 5}
GRAINS = (60, 300, 3600)
QUALITY_VERSION = 2
SCHEMA_VERSION = 15


class HistoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_name: str | None = Field(default=None, min_length=1, max_length=256)
    device_uuid: str | None = Field(default=None, min_length=1, max_length=256)
    namespace_name: str | None = Field(default=None, min_length=1, max_length=256)
    pod_name: str | None = Field(default=None, min_length=1, max_length=256)


def validate_scope(cluster, grain, dimension, entity=None):
    if cluster not in CLUSTERS or grain not in GRAINS or dimension not in ARITY:
        raise SourceError("历史集群、粒度或维度无效")
    if entity is not None and (
        not isinstance(entity, (tuple, list))
        or len(entity) != ARITY[dimension]
        or any(not isinstance(v, str) or not v or len(v.encode()) > 256 for v in entity)
    ):
        raise SourceError("历史实体键无效")
