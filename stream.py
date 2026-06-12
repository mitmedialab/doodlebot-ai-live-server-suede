"""SSE event stream — clients subscribe here to receive broadcast events."""

import asyncio, queue
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .common import add_listener, remove_listener

router = APIRouter()


@router.get("/stream")
async def stream() -> StreamingResponse:
    q = add_listener()

    async def generate() -> AsyncGenerator[str, None]:
        yield "event: connected\ndata: {}\n\n"
        try:
            while True:
                try:
                    msg = await asyncio.to_thread(q.get, True, 25)
                    yield msg
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            remove_listener(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
