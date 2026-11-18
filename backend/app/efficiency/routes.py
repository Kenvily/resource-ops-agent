"""Bounded database-only inspection endpoints."""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.clients.prometheus import SourceError
from app.efficiency.service import EfficiencyRequest

router = APIRouter(prefix="/api/v1/efficiency", tags=["efficiency"])


@router.get("/capabilities")
async def capabilities(request: Request):
    return request.app.state.efficiency.capabilities


@router.post("/investigate")
async def investigate(request: Request):
    origin = request.headers.get("origin")
    if (
        origin
        and origin.rstrip("/") != str(request.base_url).rstrip("/")
        and origin not in ("http://127.0.0.1:5173", "http://localhost:5173")
    ):
        raise HTTPException(403, "核查仅接受本站请求")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "核查请求必须使用 application/json")
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 4096:
            raise HTTPException(413, "核查请求过大")
    try:
        query = EfficiencyRequest.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "核查参数无效") from None
    service = request.app.state.efficiency
    selected = {"yz", "zw"} if query.cluster == "all" else {query.cluster}
    if not selected.issubset(service.settings.insight_allowed_clusters):
        raise HTTPException(403, "所选集群未获准进行核查")
    if query.sustained_minutes >= query.hours * 60:
        raise HTTPException(422, "持续时长须小于观察窗口")
    if service.gate.locked():
        raise HTTPException(429, "核查并发已满，请稍后重试")
    async with service.gate:
        try:
            async with asyncio.timeout(20):
                return await service.evaluate(query)
        except SourceError as error:
            raise HTTPException(503, str(error)) from None
        except Exception:  # noqa: BLE001 - sanitize private source/database details
            raise HTTPException(503, "核查暂不可用，请检查数据库和后台同步") from None


async def json_body(request, model):
    origin = request.headers.get("origin")
    if (
        origin
        and origin.rstrip("/") != str(request.base_url).rstrip("/")
        and origin not in ("http://127.0.0.1:5173", "http://localhost:5173")
    ):
        raise HTTPException(403, "仅接受本站请求")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "必须使用 application/json")
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 4096:
            raise HTTPException(413, "请求过大")
    try:
        return model.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "参数无效") from None


@router.post("/diagnose")
async def diagnose(request: Request):
    import json
    from contextlib import suppress

    from pydantic import Field

    class AgentRequest(EfficiencyRequest):
        question: str = Field(
            default="调查持续低使用的候选，核查容器信号、生命周期与替代解释", max_length=500
        )

    query = await json_body(request, AgentRequest)
    from app.investigations.routes import request_id

    task_id = request_id(request)
    service = request.app.state.efficiency
    if not service.capabilities["available"]:
        raise HTTPException(503, "使用数据采集尚未启用")
    selected = {"yz", "zw"} if query.cluster == "all" else {query.cluster}
    if not selected.issubset(service.settings.insight_allowed_clusters):
        raise HTTPException(403, "集群未授权")
    if query.sustained_minutes >= query.hours * 60:
        raise HTTPException(422, "持续时长须小于观察窗口")
    agent = service.agent
    if agent.gate.locked():
        raise HTTPException(429, "调查并发已满")
    await agent.gate.acquire()
    from fastapi.responses import StreamingResponse

    async def stream():
        queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=4)

        async def work():
            try:
                result = await request.app.state.investigations.run(
                    "gpu_usage", query, queue.put, lambda emit: agent.run(query, emit), task_id
                )
                await queue.put({"type": "result", "data": result})
            except SourceError as error:
                await queue.put({"type": "error", "data": {"message": str(error)}})
            except TimeoutError:
                await queue.put({"type": "error", "data": {"message": "调查已达到时长预算，已停止后续查询"}})
            except Exception:  # noqa: BLE001 - private source/provider/SQL details never leave boundary
                await queue.put(
                    {"type": "error", "data": {"message": "调查未完成，请检查数据覆盖或稍后重试"}}
                )
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
            agent.gate.release()

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


@router.get("/reports/{run_id}")
async def report(request: Request, run_id: str):
    from uuid import UUID

    store = request.app.state.efficiency.reports
    if not store:
        raise HTTPException(503, "需要数据库")
    try:
        result = await store.load(UUID(run_id))
        return {**result, "is_saved_report": True}
    except (ValueError, SourceError):
        raise HTTPException(404, "报告不存在或不在授权范围") from None
    except Exception:  # noqa: BLE001 - sanitized database boundary
        raise HTTPException(503, "报告读取暂不可用") from None


@router.get("/reports")
async def recent_reports(request: Request, cluster: str = "all"):
    if cluster not in ("all", "yz", "zw"):
        raise HTTPException(422, "集群无效")
    store = request.app.state.efficiency.reports
    if not store:
        raise HTTPException(503, "需要数据库")
    try:
        return await store.recent(cluster)
    except SourceError:
        raise HTTPException(403, "集群未授权") from None
    except Exception:  # noqa: BLE001 - sanitized database boundary
        raise HTTPException(503, "历史调查读取暂不可用") from None
