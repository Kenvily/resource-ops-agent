import asyncio
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.agents.routes import router as agent_router
from app.agents.service import AgentService
from app.analytics.routes import router as analytics_router
from app.analytics.service import AnalyticsService
from app.api.routes import router
from app.clients.prometheus import HttpPrometheusClient, MockPrometheusClient
from app.core.access import gateway_denial
from app.core.config import Settings, get_settings
from app.core.rate_limit import RequestLimiter
from app.core.telemetry import Telemetry
from app.efficiency.agent import EfficiencyAgent
from app.efficiency.routes import router as efficiency_router
from app.efficiency.service import EfficiencyService
from app.insights.assessment import AssessmentService
from app.insights.assessment import router as assessment_router
from app.insights.routes import router as insight_router
from app.insights.service import InsightService
from app.investigations.routes import router as investigations_router
from app.investigations.service import InvestigationService
from app.operations.host_history import HostHistoryService
from app.operations.metrics import MetricsService
from app.operations.reports import ReportService
from app.operations.routes import router as operations_router
from app.operations.workspace import WorkspaceService
from app.operations.workspace_routes import router as workspace_router
from app.resources.routes import router as resource_statistics_router
from app.resources.service import ResourceStatisticsService
from app.services.resources import ResourceService
from app.services.trends import TrendService
from app.workloads.service import WorkloadService


def create_app(settings: Settings | None = None, client=None, insight_provider=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        telemetry = Telemetry()
        app.state.telemetry = telemetry
        source = client or (
            MockPrometheusClient(settings)
            if settings.data_mode == "mock"
            else HttpPrometheusClient(settings, telemetry=telemetry)
        )
        app.state.resources = ResourceService(source, settings, telemetry)
        store = None
        sync_task = None
        snapshot_task = None
        efficiency_task = None
        operations_task = None
        history = None
        if settings.history_database_url.get_secret_value():
            from app.history.service import HistoryService
            from app.history.store import HistoryStore

            store = HistoryStore(settings)
            try:
                await store.open()
                await store.validate()
            except Exception:  # noqa: BLE001 - isolated boundary; never log private exception details
                await store.close()
                await source.aclose()
                raise RuntimeError("历史数据库连接或契约检查失败，请检查初始化与配置") from None
            from app.history.snapshots import SnapshotService

            collector = app.state.resources
            app.state.resources = SnapshotService(collector, store)
            await app.state.resources.update_all()
            snapshot_task = asyncio.create_task(app.state.resources.run())
            history = HistoryService(collector, store)
            if settings.history_sync_enabled:
                sync_task = asyncio.create_task(history.sync.run())
        app.state.history = history
        app.state.analytics = AnalyticsService(history, settings)
        app.state.investigations = InvestigationService(history, settings)
        await app.state.investigations.recover()
        app.state.workloads = WorkloadService(history, settings)
        app.state.host_history = HostHistoryService(history, settings)
        app.state.resource_statistics = ResourceStatisticsService(history, settings)
        if history:
            await app.state.resource_statistics.store.apply_bindings(settings.resource_model_mappings)
        app.state.operational_metrics = MetricsService(history, settings)
        app.state.operations = ReportService(
            app.state.resources, history, app.state.operational_metrics, settings
        )
        if history and (
            settings.operations_metrics_enabled
            or settings.host_metrics_enabled
            or settings.workload_metrics_enabled
        ):
            operations_task = asyncio.create_task(app.state.operational_metrics.run())
        app.state.efficiency = EfficiencyService(history, settings)
        if history and settings.efficiency_enabled:
            efficiency_task = asyncio.create_task(app.state.efficiency.sync.run())
        app.state.insights = InsightService(history, settings, insight_provider)
        app.state.efficiency.agent = EfficiencyAgent(
            app.state.efficiency, app.state.insights.provider, app.state.insights.runtime
        )
        app.state.agent = AgentService(
            history, settings, app.state.insights, app.state.efficiency, app.state.investigations
        )
        app.state.assessment = AssessmentService(app.state.resources, history, settings)
        app.state.workspace = WorkspaceService(
            history,
            settings,
            app.state.assessment,
            app.state.operations,
            app.state.operational_metrics,
            app.state.investigations,
        )
        app.state.trends = TrendService(app.state.resources, history)
        try:
            yield
        finally:
            for task in (sync_task, snapshot_task, efficiency_task, operations_task):
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            await app.state.insights.aclose()
            if store:
                await store.close()
            await source.aclose()

    app = FastAPI(title="GPU Resource Center", version="0.1.0", lifespan=lifespan)

    limiter = RequestLimiter(settings.api_requests_per_minute, settings.api_request_burst)
    global_limiter = RequestLimiter(settings.api_requests_per_minute * 4, settings.api_request_burst * 4)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def safe_headers(request: Request, call_next):
        denial = gateway_denial(request, settings)
        if denial is not None:
            return denial
        if request.url.path.startswith("/api/") and (
            not global_limiter.allow("application")
            or not limiter.allow(request.client.host if request.client else "unknown")
        ):
            return JSONResponse(
                {"detail": "请求过于频繁，请稍后重试"}, status_code=429, headers={"Retry-After": "2"}
            )
        allowed = {"cluster", "node", "level", "namespace", "window", "uuid", "refresh", "grain", "kind"}
        if request.url.path.startswith("/api/") and (set(request.query_params) - allowed):
            request.app.state.telemetry.record("http", "application", "rejected_parameters", 0, "error")
            return JSONResponse({"detail": "不支持的查询参数"}, status_code=422)
        if any(len(value) > 256 for value in request.query_params.values()):
            request.app.state.telemetry.record("http", "application", "rejected_parameters", 0, "error")
            return JSONResponse({"detail": "查询参数过长"}, status_code=422)
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            request.app.state.telemetry.record(
                "http", "application", "internal_error", time.monotonic() - started, "error"
            )
            raise
        route = getattr(request.scope.get("route"), "path", "static_or_unknown")
        request.app.state.telemetry.record(
            "http",
            "application",
            route,
            time.monotonic() - started,
            "error" if response.status_code >= 400 else "ok",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app.include_router(router)
    app.include_router(agent_router)
    app.include_router(analytics_router)
    app.include_router(investigations_router)
    app.include_router(insight_router)
    app.include_router(assessment_router)
    app.include_router(efficiency_router)
    app.include_router(operations_router)
    app.include_router(workspace_router)
    app.include_router(resource_statistics_router)
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
