"""Closed database statistics APIs; no source URLs, PromQL or model writes."""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.clients.prometheus import SourceError
from app.resources.models import DeviceHistoryRequest, StatisticsRequest

router = APIRouter(prefix="/api/v1/resource-statistics", tags=["resource-statistics"])


async def body(request, model):
    origin = request.headers.get("origin")
    if (
        origin
        and origin.rstrip("/") != str(request.base_url).rstrip("/")
        and origin not in ("http://127.0.0.1:5173", "http://localhost:5173")
    ):
        raise HTTPException(403, "仅接受本站请求")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "需要 application/json")
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > 4096:
            raise HTTPException(413, "请求过大")
    try:
        return model.model_validate_json(data)
    except ValidationError:
        raise HTTPException(422, "统计参数无效") from None


@router.post("")
async def statistics(request: Request):
    query = await body(request, StatisticsRequest)
    selected = {"yz", "zw"} if query.cluster == "all" else {query.cluster}
    if not selected.issubset(request.app.state.resource_statistics.settings.insight_allowed_clusters):
        raise HTTPException(403, "所选集群未获准查询")
    try:
        async with asyncio.timeout(15):
            return await request.app.state.resource_statistics.statistics(query)
    except SourceError as error:
        raise HTTPException(503, str(error)) from None
    except Exception:  # noqa: BLE001 - sanitize private database errors
        raise HTTPException(503, "资源统计数据库暂不可用") from None


@router.post("/history")
async def history(request: Request):
    query = await body(request, DeviceHistoryRequest)
    selected = {"yz", "zw"} if query.cluster == "all" else {query.cluster}
    if not selected.issubset(request.app.state.resource_statistics.settings.insight_allowed_clusters):
        raise HTTPException(403, "所选集群未获准查询")
    try:
        async with asyncio.timeout(15):
            return await request.app.state.resource_statistics.history_for_device(query)
    except SourceError as error:
        raise HTTPException(422, str(error)) from None
    except Exception:  # noqa: BLE001 - sanitize private database errors
        raise HTTPException(503, "设备历史数据库暂不可用") from None


@router.post("/host")
async def host(request: Request):
    from app.operations.host_history import HostRequest

    query = await body(request, HostRequest)
    service = request.app.state.host_history
    if query.cluster not in service.settings.insight_allowed_clusters:
        raise HTTPException(403, "所选集群未获准查询")
    try:
        async with asyncio.timeout(15):
            return await service.detail(query)
    except SourceError as error:
        raise HTTPException(422, str(error)) from None
    except Exception:  # noqa: BLE001 - keep private database/source details out of API errors
        raise HTTPException(503, "主机资源数据库暂不可用") from None


@router.post("/workloads")
async def workloads(request: Request):
    query = await body(request, StatisticsRequest)
    service = request.app.state.workloads
    selected = {"yz", "zw"} if query.cluster == "all" else {query.cluster}
    if not selected.issubset(service.settings.insight_allowed_clusters):
        raise HTTPException(403, "所选集群未获准查询")
    try:
        async with asyncio.timeout(15):
            return await service.read(query.cluster)
    except SourceError as error:
        raise HTTPException(503, str(error)) from None
    except Exception:  # noqa: BLE001 - never expose private database/source labels
        raise HTTPException(503, "业务资源数据库暂不可用") from None


@router.post("/trends")
async def fleet_history(request: Request):
    from app.resources.trends import FleetTrendRequest, fleet_trends

    query = await body(request, FleetTrendRequest)
    service = request.app.state.resource_statistics
    selected = {"yz", "zw"} if query.cluster == "all" else {query.cluster}
    if not selected.issubset(service.settings.insight_allowed_clusters):
        raise HTTPException(403, "所选集群未获准查询")
    try:
        async with asyncio.timeout(15):
            return await fleet_trends(service, query)
    except SourceError as error:
        raise HTTPException(503, str(error)) from None
    except Exception:  # noqa: BLE001 - sanitize private database errors
        raise HTTPException(503, "多资源历史数据库暂不可用") from None
