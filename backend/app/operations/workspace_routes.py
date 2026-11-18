"""Local-development metadata writes. These endpoints never change cluster resources."""

import asyncio
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.efficiency.routes import json_body
from app.insights.assessment import AssessmentRequest
from app.models.resource import ClusterFilter
from app.operations.reports import ReportRequest
from app.operations.workspace import WorkspaceError

router = APIRouter(prefix="/api/v1/operations/workspace", tags=["operations-workspace"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID


class SaveAssessment(Input):
    requirement: AssessmentRequest


class SaveReport(Input):
    report: ReportRequest
    investigation_ids: list[UUID] = Field(default_factory=list, max_length=5)
    issue_ids: list[UUID] = Field(default_factory=list, max_length=5)


class Decision(Input):
    decision: Literal["accepted", "needs_changes", "rejected"]
    candidates: list[int] = Field(default_factory=list, max_length=3)
    note: str = Field(default="", max_length=1000)


class CaptureIssue(Input):
    cluster: Literal["yz", "zw"]
    metric: Literal["gpu_temp", "gpu_xid", "gpu_ecc", "node_ready"]
    node: str = Field(min_length=1, max_length=256)
    device_uuid: str | None = Field(default=None, max_length=256)


class IssueUpdate(Input):
    revision: int = Field(ge=1, le=200, strict=True)
    status: Literal["open", "investigating", "resolved", "dismissed"]
    note: str = Field(default="", max_length=1000)
    investigation_id: UUID | None = None


def local_write(request):
    if not request.app.state.workspace.settings.operations_workspace_enabled:
        raise HTTPException(403, "运营记录写入未启用")
    if (
        not request.client
        or request.client.host not in ("127.0.0.1", "::1")
        or any(h in request.headers for h in ("forwarded", "x-forwarded-for"))
    ):
        raise HTTPException(403, "记录写入仅开放本机直连；对外部署需认证授权")


async def run(request, work):
    service = request.app.state.workspace
    if service.gate.locked():
        raise HTTPException(429, "运营记录服务繁忙，请稍后重试")
    async with service.gate:
        try:
            async with asyncio.timeout(30):
                return await work()
        except WorkspaceError as error:
            raise HTTPException(error.status, str(error)) from None
        except Exception:  # noqa: BLE001 - no private SQL, labels or upstream details
            raise HTTPException(503, "运营记录暂不可用，请检查数据库或重新读取") from None


@router.get("/capabilities")
async def capabilities(request: Request):
    service = request.app.state.workspace
    return {
        "available": bool(service.pool),
        "write_enabled": bool(service.pool) and service.settings.operations_workspace_enabled,
    }


@router.post("/assessments")
async def assessment(request: Request):
    local_write(request)
    query = await json_body(request, SaveAssessment)
    return await run(request, lambda: request.app.state.workspace.create_record("assessment", query))


@router.post("/reports")
async def report(request: Request):
    local_write(request)
    query = await json_body(request, SaveReport)
    if len(set(query.investigation_ids)) != len(query.investigation_ids) or len(set(query.issue_ids)) != len(
        query.issue_ids
    ):
        raise HTTPException(422, "关联记录不可重复")
    return await run(request, lambda: request.app.state.workspace.create_record("report", query))


@router.get("/records")
async def recent(request: Request, kind: Literal["assessment", "report"], cluster: ClusterFilter = "all"):
    return await run(request, lambda: request.app.state.workspace.recent(kind, cluster))


@router.get("/records/{record_id}")
async def detail(request: Request, record_id: UUID, cluster: ClusterFilter = "all"):
    return await run(request, lambda: request.app.state.workspace.detail(record_id, cluster))


@router.post("/records/{record_id}/review")
async def review(request: Request, record_id: UUID):
    local_write(request)
    query = await json_body(request, Decision)
    if any(i < 0 for i in query.candidates) or len(set(query.candidates)) != len(query.candidates):
        raise HTTPException(422, "候选索引无效")
    return await run(request, lambda: request.app.state.workspace.review(record_id, query))


@router.get("/issues")
async def issues(request: Request, cluster: ClusterFilter = "all"):
    return await run(request, lambda: request.app.state.workspace.issues(cluster))


@router.post("/issues")
async def capture(request: Request):
    local_write(request)
    query = await json_body(request, CaptureIssue)
    return await run(request, lambda: request.app.state.workspace.capture_issue(query))


@router.get("/issues/{issue_id}")
async def issue(request: Request, issue_id: UUID, cluster: ClusterFilter = "all"):
    return await run(request, lambda: request.app.state.workspace.issue_detail(issue_id, cluster))


@router.post("/issues/{issue_id}/review")
async def update(request: Request, issue_id: UUID):
    local_write(request)
    query = await json_body(request, IssueUpdate)
    return await run(request, lambda: request.app.state.workspace.transition(issue_id, query))
