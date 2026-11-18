from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.history.contracts import Dimension
from app.models.history_analysis import AnalysisWindow
from app.models.resource import ClusterFilter, ClusterId, Units
from app.models.trend import Window


class DiagnosisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster: ClusterFilter = "all"
    window: Window = "24h"
    start: int | None = Field(default=None, gt=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def custom_pair(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("自定义范围必须同时指定起止时间")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("结束时间须晚于开始时间")
        return self

    question: str = Field(default="分析所选时间范围的资源分配变化，核查主要贡献与替代解释。", max_length=500)


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cluster: ClusterId | None = None
    segment: Literal["full", "early", "late"] = "full"
    dimension: Literal["node", "gpu", "namespace", "pod"] = "namespace"
    parent_entity_id: str | None = Field(default=None, max_length=40)
    entity_id: str | None = Field(default=None, max_length=40)


EvidenceStatus = Literal["complete", "limited", "error"]
DiagnosisMode = Literal["deepseek", "deterministic", "degraded"]
DiagnosisStatus = Literal["complete", "limited", "budget_exhausted", "refused", "unavailable"]
FindingKind = Literal[
    "allocation_increase",
    "capacity_drop",
    "capacity_increase",
    "mixed",
    "stable",
    "allocation_decrease",
    "insufficient",
]


class Evidence(BaseModel):
    provenance: dict = Field(default_factory=dict)
    id: str
    tool: str
    label: str
    cluster: ClusterId | None = None
    status: EvidenceStatus
    data: dict
    elapsed_ms: float
    arguments: dict = Field(default_factory=dict)


class Finding(BaseModel):
    cluster: ClusterId
    kind: FindingKind
    title: str
    evidence_ids: list[str]
    capacity_change_raw: float | None = None
    allocated_change_raw: float | None = None
    baseline_ratio: float | None = None
    target_ratio: float | None = None
    limitations: list[str] = Field(default_factory=list)


class EntityReference(BaseModel):
    id: str
    cluster: ClusterId
    dimension: Dimension
    key: tuple[str, ...]
    label: str


class DiagnosisResult(BaseModel):
    execution: dict = Field(default_factory=dict)
    id: str
    mode: DiagnosisMode
    status: DiagnosisStatus
    windows: dict[str, dict[str, AnalysisWindow]]
    units: Units
    findings: list[Finding]
    evidence: list[Evidence]
    entities: list[EntityReference]
    warnings: list[str]
    tool_calls: int
    model_calls: int
    elapsed_ms: float
