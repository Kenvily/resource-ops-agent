"""Readiness checks persisted facts only, never waits for an upstream collection."""

import asyncio
import time

from app.history.snapshots import SnapshotService


async def readiness(history, settings):
    if not history:
        return {"ready": False, "database": "not_configured", "clusters": []}
    try:
        async with asyncio.timeout(5):
            snapshots = SnapshotService(history.resources, history.store)
            sources = []
            for cid in settings.insight_allowed_clusters:
                row = await history.store.snapshot(cid)
                value = snapshots.present(cid, time.time(), row, True)
                sources.append({"cluster": cid, "status": value.status})
            return {
                "ready": bool(sources) and all(s["status"] == "healthy" for s in sources),
                "database": "available",
                "clusters": sources,
            }
    except Exception:  # noqa: BLE001 - no private database/source details
        return {"ready": False, "database": "unavailable", "clusters": []}
