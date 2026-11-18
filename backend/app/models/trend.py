from typing import Literal

from pydantic import BaseModel, Field

from app.models.resource import ClusterId, DataStatus, Units

Window = Literal["1h", "1d", "1w", "1mo", "3mo", "all", "24h", "7d", "4mo"]


class TrendPoint(BaseModel):
    timestamp: int
    capacity_raw: float | None = None
    allocated_raw: float | None = None
    unallocated_raw: float | None = None
    allocation_ratio: float | None = None


class TrendSeries(BaseModel):
    source_start_at: int | None = None
    database_read_at: float | None = None
    history_collected_at: float | None = None
    expected_points: int | None = None
    observed_points: int | None = None
    missing_points: int | None = None
    invalid_points: int | None = None
    id: ClusterId
    label: str
    status: DataStatus
    points: list[TrendPoint] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TrendResponse(BaseModel):
    mode: Literal["mock", "live"]
    units: Units
    window: Window
    start: int
    end: int
    step: int
    fetched_at: float
    complete: bool
    cache_status: Literal["miss", "hit"] = "miss"
    cache_age_seconds: float = 0
    series: list[TrendSeries]
