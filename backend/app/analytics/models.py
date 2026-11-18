from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Page = Literal[
    "workbench",
    "assets.servers",
    "assets.gpus",
    "workloads.business_models",
    "analysis.history",
    "analysis.assistant",
    "analysis.diagnosis",
    "analysis.efficiency",
    "analysis.records",
    "planning",
    "operations.reports",
    "operations.monitoring",
    "operations.issues",
    "settings.status",
    "settings.infrastructure",
    "settings.usage",
]
Feature = Literal[
    "assistant_query",
    "diagnosis_run",
    "efficiency_run",
    "planning_assess",
    "report_generate",
    "report_export",
    "history_change",
    "manual_refresh",
]
FEATURE_PAGES = {
    "assistant_query": {"analysis.assistant"},
    "diagnosis_run": {"analysis.diagnosis"},
    "efficiency_run": {"analysis.efficiency"},
    "planning_assess": {"planning"},
    "report_generate": {"operations.reports"},
    "report_export": {"operations.reports"},
    "history_change": {"analysis.history"},
}


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    page: Page
    kind: Literal["page_view", "feature_use"]
    feature: Feature | None = None

    @model_validator(mode="after")
    def check_feature(self):
        if (self.kind == "feature_use") != (self.feature is not None):
            raise ValueError("事件类型与功能不一致")
        if self.feature in FEATURE_PAGES and self.page not in FEATURE_PAGES[self.feature]:
            raise ValueError("功能与页面不一致")
        return self


class EventBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_id: UUID
    events: list[Event] = Field(min_length=1, max_length=10)
