from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.clients.prometheus import SourceError
from app.efficiency.routes import json_body
from app.models.resource import ClusterFilter

router = APIRouter(prefix="/api/v1/investigations", tags=["investigations"])


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    decision: Literal["accepted", "follow_up", "insufficient"]
    note: str = Field(default="", max_length=1000)


def request_id(request):
    value = request.headers.get("idempotency-key")
    try:
        return UUID(value) if value else None
    except ValueError:
        raise HTTPException(422, "任务标识须为 UUID") from None


@router.get("")
async def recent(request: Request, cluster: ClusterFilter = "all"):
    service = request.app.state.investigations
    try:
        return await service.recent(cluster)
    except SourceError as error:
        raise HTTPException(403, str(error)) from None
    except Exception:  # noqa: BLE001 - sanitized persistence boundary
        raise HTTPException(503, "调查记录暂不可用") from None


@router.get("/{task_id}")
async def detail(request: Request, task_id: UUID, cluster: ClusterFilter = "all"):
    try:
        return await request.app.state.investigations.detail(task_id, cluster)
    except SourceError:
        raise HTTPException(404, "调查不存在或不在授权范围") from None
    except Exception:  # noqa: BLE001 - sanitized persistence boundary
        raise HTTPException(503, "调查记录暂不可用") from None


@router.post("/{task_id}/feedback")
async def feedback(request: Request, task_id: UUID):
    if (
        not request.client
        or request.client.host not in ("127.0.0.1", "::1")
        or any(h in request.headers for h in ("forwarded", "x-forwarded-for"))
    ):
        raise HTTPException(403, "反馈仅开放本机调试；代理部署须另行接入认证授权")
    query = await json_body(request, FeedbackRequest)
    try:
        return await request.app.state.investigations.feedback(task_id, query)
    except SourceError as error:
        raise HTTPException(403, str(error)) from None
    except Exception:  # noqa: BLE001 - sanitized persistence boundary
        raise HTTPException(503, "反馈保存暂不可用") from None
