"""Same-origin bounded NDJSON entry; disconnect cancels model, tools and task persistence."""

import asyncio
import json
from contextlib import suppress

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.agents.models import AgentQuery
from app.clients.prometheus import SourceError
from app.efficiency.routes import json_body
from app.investigations.routes import request_id

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


@router.post("/query")
async def query(request: Request):
    body = await json_body(request, AgentQuery)
    service = request.app.state.agent
    try:
        await service.prepare(body)
    except SourceError as error:
        raise HTTPException(422, str(error)) from None
    task_id = request_id(request)
    if service.insights.gate.locked():
        raise HTTPException(429, "调查并发已满，请稍后重试")
    await service.insights.gate.acquire()

    async def stream():
        queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=4)

        async def work():
            try:
                result = await service.run(body, queue.put, task_id)
                await queue.put({"type": "result", "data": result})
            except SourceError as error:
                await queue.put({"type": "error", "data": {"message": str(error)}})
            except Exception:  # noqa: BLE001 - no private labels, SQL or provider details
                await queue.put(
                    {"type": "error", "data": {"message": "资源调查未完成，已保存的步骤可在记录中查看"}}
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
            service.insights.gate.release()

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )
