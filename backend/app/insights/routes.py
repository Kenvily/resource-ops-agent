"""Bounded diagnosis streams; disconnect cancels the model and database task."""

import asyncio
import json
import time
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.clients.prometheus import SourceError
from app.insights.models import DiagnosisRequest
from app.insights.tools import windows_for

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])


@router.get("/capabilities")
async def capabilities(request: Request):
    return request.app.state.insights.capabilities


@router.post("/diagnose")
async def diagnose(request: Request):
    origin = request.headers.get("origin")
    if (
        origin
        and origin.rstrip("/") != str(request.base_url).rstrip("/")
        and origin not in ("http://127.0.0.1:5173", "http://localhost:5173")
    ):
        raise HTTPException(403, "诊断仅接受本站请求")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "诊断请求必须使用 application/json")
    # Limit actual bytes rather than trusting Content-Length.
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 8192:
            raise HTTPException(413, "诊断请求过大")
    try:
        diagnosis = DiagnosisRequest.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "诊断参数无效，仅支持预设集群和时间范围") from None
    from app.investigations.routes import request_id

    task_id = request_id(request)
    service = request.app.state.insights
    if not service.history:
        raise HTTPException(503, "诊断需要已配置的 PostgreSQL 历史库")
    try:
        windows_for(diagnosis, int(time.time()), service.settings.insight_timeout_seconds)
    except SourceError as reason:
        raise HTTPException(422, str(reason)) from None
    selected = {"yz", "zw"} if diagnosis.cluster == "all" else {diagnosis.cluster}
    if not selected.issubset(service.settings.insight_allowed_clusters):
        raise HTTPException(403, "所选集群未获准进行诊断")
    if service.gate.locked():
        raise HTTPException(429, "诊断并发已满，请稍后重试")
    await service.gate.acquire()

    async def stream():
        queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=4)

        async def work():
            try:
                result = await request.app.state.investigations.run(
                    "allocation_change",
                    diagnosis,
                    queue.put,
                    lambda emit: service.run(diagnosis, emit),
                    task_id,
                )
                await queue.put({"type": "result", "data": result})
            except SourceError:
                await queue.put(
                    {"type": "error", "data": {"message": "历史证据暂不可用，请检查数据库与所选范围"}}
                )
            except Exception:  # noqa: BLE001 - keep credentials, database and provider details private
                await queue.put({"type": "error", "data": {"message": "诊断暂不可用，已停止后续查询"}})
            await queue.put(None)

        task = asyncio.create_task(work())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            service.gate.release()

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )
