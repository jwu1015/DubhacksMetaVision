"""The projector page (/stream/viewer), its video and its events."""
import asyncio
from contextlib import suppress

from fastapi import APIRouter, WebSocket
from fastapi.responses import FileResponse, StreamingResponse

from storefront import config
from storefront.live import stream

router = APIRouter(prefix="/stream")


@router.get("/viewer", include_in_schema=False)
async def viewer_route():
    return FileResponse(config.STATIC / "viewer.html")


@router.get("/video.mjpg")
async def video_route():
    return StreamingResponse(stream.live.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@router.websocket("/events")
async def events_route(ws: WebSocket):
    """The JSON events, starting with the current state, until the client goes away."""
    await ws.accept()

    async def send_all():
        with suppress(Exception):  # the client left mid-send
            async for event in stream.live.subscribe():
                await ws.send_json(event)

    sender = asyncio.create_task(send_all())
    try:
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass
    finally:
        sender.cancel()
