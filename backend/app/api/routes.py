from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from app.clients.prometheus import SourceError
from app.history.snapshots import SnapshotUnavailable
from app.models.resource import ClusterFilter, ClusterId, Overview
from app.models.trend import TrendResponse, Window

router = APIRouter()


async def snapshot(request: Request, cluster="all", refresh: bool = False):
    try:
        return await request.app.state.resources.overview(cluster, refresh)
    except SourceError:
        raise HTTPException(503, "资源数据库暂时不可用，请稍后重试") from None


@router.get("/api/health")
async def health(request: Request):
    data = await snapshot(request)
    return {
        "service": "ok",
        "mode": data.mode,
        "overall_status": data.overall_status,
        "sources": [
            {"id": c.id, "status": c.status, "sampled_at": c.sampled_at, "warnings": c.warnings}
            for c in data.clusters
        ],
    }


@router.get("/api/v1/overview", response_model=Overview)
async def overview(request: Request, cluster: ClusterFilter = "all", refresh: bool = False):
    return await snapshot(request, cluster, refresh)


@router.get("/api/v1/clusters")
async def clusters(request: Request):
    data = await snapshot(request)
    return {
        "mode": data.mode,
        "clusters": [c.model_dump(exclude={"gpus", "workloads"}) for c in data.clusters],
    }


@router.get("/api/v1/nodes")
async def nodes(request: Request, cluster: ClusterFilter = "all"):
    data = await snapshot(request, cluster)
    return {
        "mode": data.mode,
        "status": data.overall_status,
        "units": data.units,
        "fetched_at": data.fetched_at,
        "complete": data.complete,
        "nodes": data.nodes,
    }


@router.get("/api/v1/gpus")
async def gpus(request: Request, cluster: ClusterFilter = "all", node: str | None = None):
    data = await snapshot(request, cluster)
    return {
        "mode": data.mode,
        "units": data.units,
        "fetched_at": data.fetched_at,
        "complete": data.complete,
        "status": data.overall_status,
        "gpus": [g for c in data.clusters for g in c.gpus if node is None or g.node == node],
    }


@router.get("/api/v1/gpus/{cluster}/{node}/{uuid}")
async def gpu_detail(request: Request, cluster: ClusterId, node: str, uuid: str):
    data = await snapshot(request, cluster)
    c = data.clusters[0]
    for gpu in c.gpus:
        if gpu.node == node and gpu.device_uuid == uuid:
            return {
                "mode": data.mode,
                "units": data.units,
                "fetched_at": data.fetched_at,
                "status": c.status,
                "gpu": gpu,
                "workloads": [w for w in c.workloads if w.node == node and w.device_uuid == uuid],
            }
    raise HTTPException(404, "未找到设备；请确认数据源状态")


@router.get("/api/v1/workloads")
async def workloads(
    request: Request,
    cluster: ClusterFilter = "all",
    level: Literal["namespace", "pod"] = "namespace",
    namespace: str | None = None,
):
    data = await snapshot(request, cluster)
    groups = defaultdict(list)
    for c in data.clusters:
        for row in c.workloads:
            if namespace is None or row.namespace == namespace:
                groups[(row.cluster, row.namespace, row.pod if level == "pod" else None)].append(row)
    items = []
    for (cid, ns, pod), rows in groups.items():
        values = [r.allocated_raw for r in rows]
        items.append(
            {
                "cluster": cid,
                "namespace": ns,
                "pod": pod,
                "allocated_raw": sum(values) if all(v is not None for v in values) else None,
                "gpu_count": len({(r.node, r.device_uuid) for r in rows}),
                "records": rows,
            }
        )
    return {
        "mode": data.mode,
        "units": data.units,
        "fetched_at": data.fetched_at,
        "complete": data.complete,
        "status": data.overall_status,
        "items": items,
    }


@router.get("/api/v1/trends", response_model=TrendResponse)
async def trends(
    request: Request,
    cluster: ClusterFilter = "all",
    window: Window = "24h",
    node: str | None = None,
    uuid: str | None = None,
    refresh: bool = False,
):
    if bool(node) != bool(uuid) or (node is not None and cluster == "all"):
        raise HTTPException(422, "单卡历史必须指定一个集群、节点和设备")
    try:
        return await request.app.state.trends.query(
            cluster, window, (node, uuid) if node and uuid else None, refresh
        )
    except SnapshotUnavailable:
        raise HTTPException(503, "资源数据库暂时不可用，请稍后重试") from None
    except SourceError as error:
        raise HTTPException(404, str(error)) from None


@router.get("/api/v1/diagnostics")
async def diagnostics(request: Request):
    result = request.app.state.telemetry.snapshot()
    history = request.app.state.history
    result["efficiency"] = request.app.state.efficiency.capabilities
    result["operations_collector"] = request.app.state.operational_metrics.diagnostics()
    if history:
        result["snapshots"] = {"sync_status": request.app.state.resources.sync_status}
        try:
            result["history"] = {
                "sync_status": history.sync.last_status,
                "scopes": history.sync.scope_status,
                "progress": await history.store.status(),
            }
        except Exception:  # noqa: BLE001 - sanitized diagnostic boundary
            result["history"] = {"sync_status": "database_unavailable"}
    return result


@router.get("/api/v1/history/statistics")
async def history_statistics(
    request: Request,
    cluster: ClusterId,
    window: Window = "1mo",
    grain: Literal["60", "300", "3600"] | None = None,
):
    import time

    from app.core.time_ranges import range_seconds
    from app.history.sync import grain_for

    history = request.app.state.history
    if not history:
        raise HTTPException(503, "历史统计需要数据库")
    selected_grain = grain_for(window) if grain is None else int(grain)
    end = (int(time.time()) - 120) // selected_grain * selected_grain
    try:
        first = await history.earliest(cluster) if window == "all" else end - range_seconds(window, end)
        start = ((first + selected_grain - 1) // selected_grain) * selected_grain
        report = (
            await history.window_statistics(cluster, selected_grain, start, end)
            if grain is None
            else await history.statistics(cluster, selected_grain, start, end)
        )
        report.setdefault("warnings", [])
        if window == "all" and not await history.discovery_complete(cluster):
            report["warnings"].append("源端起点未完整核验，统计仅覆盖已发现范围")
        return report
    except SourceError:
        raise HTTPException(503, "历史统计暂不可用") from None


@router.get("/api/ready")
async def ready(request: Request):
    from fastapi.responses import JSONResponse

    from app.validation.readiness import readiness

    result = await readiness(request.app.state.history, request.app.state.resources.settings)
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


@router.get("/api/live")
async def live():
    return {"service": "ok"}
