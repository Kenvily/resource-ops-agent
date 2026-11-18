import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from app.analytics.models import EventBatch
from app.clients.prometheus import SourceError
from app.efficiency.routes import json_body

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


@router.post("/events")
async def events(request: Request):
    batch = await json_body(request, EventBatch)
    try:
        async with asyncio.timeout(4):
            return await request.app.state.analytics.record(batch)
    except SourceError as error:
        raise HTTPException(503, str(error)) from None
    except Exception:  # noqa: BLE001 - no browser identifiers or DB errors in responses
        raise HTTPException(503, "使用统计暂不可用") from None


@router.get("/summary")
async def summary(request: Request, window: Literal["1d", "7d", "30d", "90d"] = "7d"):
    try:
        async with asyncio.timeout(8):
            return await request.app.state.analytics.summary(int(window[:-1]))
    except SourceError as error:
        raise HTTPException(503, str(error)) from None
    except Exception:  # noqa: BLE001 - no private database failures
        raise HTTPException(503, "使用统计暂不可用") from None
