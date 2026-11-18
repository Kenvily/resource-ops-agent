"""Deterministic history evidence, not scheduling or physical-utilization conclusions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.history.contracts import Dimension, HistoryScope
from app.models.resource import ClusterId, Units


class AnalysisWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int = Field(gt=0)
    end: int = Field(gt=0)
    grain: Literal[60, 300, 3600]
    step: int | None = Field(default=None, ge=60)

    @model_validator(mode="after")
    def aligned(self):
        self.step = self.step or self.grain
        if (
            self.end < self.start
            or self.start % self.grain
            or self.end % self.grain
            or self.step % self.grain
            or (self.end - self.start) % self.step
        ):
            raise ValueError("analysis window must use an exact aligned grid")
        if (self.end - self.start) // self.step + 1 > 4096:
            raise ValueError("analysis window exceeds point budget")
        return self

    @property
    def expected_points(self):
        return (self.end - self.start) // (self.step or self.grain) + 1


class PeriodMetrics(BaseModel):
    expected_points: int
    observed_points: int
    absent_points: int = 0
    covered_points: int = 0
    verified_coverage_points: int = 0
    zero_inferred: bool = False
    complete: bool
    capacity_mean_raw: float | None = None
    allocated_mean_raw: float | None = None
    allocated_max_raw: float | None = None
    unallocated_min_raw: float | None = None
    ratio_mean: float | None = None


class ChangeEvidence(BaseModel):
    entity: tuple[str, ...]
    baseline: PeriodMetrics
    target: PeriodMetrics
    allocated_change_raw: float | None = None
    capacity_change_raw: float | None = None


class HistoryComparison(BaseModel):
    cluster: ClusterId
    dimension: Dimension
    scope: HistoryScope
    baseline_window: AnalysisWindow
    target_window: AnalysisWindow
    units: Units
    complete: bool
    truncated: bool = False
    entity_count: int | None = None
    omitted_entities: int | None = None
    cohort_allocated_change_raw: float | None = None
    changes: list[ChangeEvidence]
    warnings: list[str] = Field(default_factory=list)
