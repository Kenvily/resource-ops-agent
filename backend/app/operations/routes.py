"""Manual reports and read-only monitoring. No arbitrary queries or resource writes."""

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.clients.prometheus import SourceError
from app.models.resource import ClusterFilter
from app.operations.infrastructure import infrastructure
from app.operations.metrics import alerts
from app.operations.reports import ReportRequest

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


def scope(request, cluster):
    selected = ["yz", "zw"] if cluster == "all" else [cluster]
    if not set(selected).issubset(request.app.state.operations.settings.insight_allowed_clusters):
        raise HTTPException(403, "所选集群未获准查询")
    return selected


@router.post("/reports")
async def report(request: Request):
    origin = request.headers.get("origin")
    if (
        origin
        and origin.rstrip("/") != str(request.base_url).rstrip("/")
        and origin not in ("http://127.0.0.1:5173", "http://localhost:5173")
    ):
        raise HTTPException(403, "仅接受本站请求")
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(415, "需要 application/json")
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 2048:
            raise HTTPException(413, "请求过大")
    try:
        query = ReportRequest.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "报告参数无效") from None
    scope(request, query.cluster)
    service = request.app.state.operations
    if service.gate.locked():
        raise HTTPException(429, "报告生成并发已满，请稍后重试")
    async with service.gate:
        try:
            async with asyncio.timeout(20):
                return await service.generate(query)
        except SourceError as error:
            raise HTTPException(503, str(error)) from None
        except Exception:  # noqa: BLE001 - sanitize private database failures
            raise HTTPException(503, "报告暂不可用，请检查数据库与后台同步") from None


@router.get("/monitoring")
async def monitoring(request: Request, cluster: ClusterFilter = "all"):
    selected = scope(request, cluster)
    service = request.app.state.operational_metrics
    try:
        async with asyncio.timeout(10):
            metrics = await service.read(selected)
    except Exception:  # noqa: BLE001 - sanitize private database failures
        raise HTTPException(503, "指标数据库暂不可用") from None
    events = alerts(metrics, service.settings.operations_gpu_temperature_c)
    from app.operations.hosts import read_hosts

    hosts = await read_hosts(service.history, selected)
    return {
        "hosts": hosts,
        "host_collection_enabled": service.settings.host_metrics_enabled,
        "mode": service.settings.data_mode,
        "database_read_at": time.time(),
        "collection_enabled": service.settings.operations_metrics_enabled,
        "metrics": metrics,
        "alerts": events[:200],
        "alert_count": len(events),
        "truncated": len(events) > 200,
        "health": "attention"
        if events
        else "unknown"
        if any(m["status"] != "ready" for m in metrics)
        else "no_rule_triggered",
        "rules": [
            "GPU 温度达到配置阈值",
            "最近 XID 编号非零（需核验发生时间）",
            "累计双位 ECC 错误非零（不表示本次新增）",
            "GPU 节点 Ready 不成立",
        ],
        "temperature_threshold_c": service.settings.operations_gpu_temperature_c,
    }


@router.get("/infrastructure")
async def capacity(request: Request):
    try:
        async with asyncio.timeout(10):
            return await infrastructure(request.app.state.history, request.app.state.telemetry)
    except Exception:  # noqa: BLE001 - sanitize private database failures
        raise HTTPException(503, "运行状态暂不可用") from None


@router.get("/host-sync")
async def host_sync(request: Request, cluster: ClusterFilter = "all"):
    selected = scope(request, cluster)
    try:
        async with asyncio.timeout(10):
            return await request.app.state.host_history.status(selected)
    except Exception:  # noqa: BLE001 - no private database/source labels
        raise HTTPException(503, "主机同步状态暂不可用") from None
