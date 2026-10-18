"""The browser WebRTC viewer: the phone's live video, relayed over WebRTC, with the tracked boxes on top.

    GET /rtc/viewer   the page (static/rtc_viewer.html)
    WS  /rtc/ws       signaling and results, JSON both ways

The server says, on connect and then whenever it changes:

    {"type": "hello", "iceServers": [...], "rotate": 0, "live": true}   rotate: as WEBRTC_ROTATE (degrees
                                                                        counterclockwise, as PIL's rotate)
    {"type": "live", "live": true | false}       a phone started or stopped sending video
    {"type": "status", "tracking": "stopped" | "starting" | "ok" | "offline"}
    {"type": "boxes", "width", "height", "age_ms",
     "objects": [{"track_id": "t3", "label": "sofa", "score": 0.93, "box": [x1, y1, x2, y2]}]}
                                                 the objects in view, boxes normalized 0-1 of the upright frame

The browser, while live: {"type": "offer", "sdp"} with one recvonly video transceiver, then its ICE
candidates as it finds them, {"type": "candidate", "candidate", "sdpMid", "sdpMLineIndex"}, and
{"type": "candidate", "candidate": null} when done. The server answers {"type": "answer", "sdp"} with
all its own candidates in it (aiortc gathers before answering), or {"type": "error", "message"}.
When the phone goes away the server closes the peer connection and says "live": false; when one comes
back it says "live": true and the browser offers again.
"""
import asyncio
import json
import logging
from contextlib import suppress

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import stream
import webrtc

log = logging.getLogger("gtl.rtc_viewer")

MAX_VIEWERS = 4  # each viewer costs a video encode on this laptop
OUTBOX = 32      # messages waiting for a slow browser; past this, boxes are skipped


def boxes(event: dict) -> dict:
    """A stream "objects" event as a boxes message: the objects in view only."""
    return {"type": "boxes", "width": event["width"], "height": event["height"], "age_ms": event["age_ms"],
            "objects": [{"track_id": f"t{o['id']}", "label": o["label"], "score": o["score"],
                         "box": webrtc.corners(o["box"])}
                        for o in event["objects"] if o.get("in_view", True)]}


class Viewer:
    """One browser: its WebSocket, and its peer connection while the phone is live."""

    def __init__(self, ws: WebSocket, n: int):
        self.ws, self.n = ws, n
        self.pc: RTCPeerConnection | None = None
        self.outbox: asyncio.Queue = asyncio.Queue(OUTBOX)
        self._closing: asyncio.Task | None = None

    def __str__(self):
        return f"viewer {self.n}"

    def send(self, msg: dict):
        """Queue a message; a browser that falls behind loses boxes, never anything else."""
        if self.outbox.full():
            if msg["type"] == "boxes":
                return
            with suppress(asyncio.QueueEmpty):
                self.outbox.get_nowait()
        self.outbox.put_nowait(msg)

    async def run(self):
        live = webrtc.phones.live
        self.send({"type": "hello", "iceServers": await webrtc.phones.ice.get(), "rotate": webrtc.ROTATE,
                   "live": live})

        def video_changed():
            nonlocal live
            if webrtc.phones.live != live:
                live = webrtc.phones.live
                if not live:
                    self._closing = asyncio.create_task(self.close_pc())  # its track has ended
                self.send({"type": "live", "live": live})
        webrtc.phones.listeners.add(video_changed)
        tasks = [asyncio.create_task(self._write()), asyncio.create_task(self._results())]
        try:
            while True:
                await self._on_message(await self.ws.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            webrtc.phones.listeners.discard(video_changed)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.close_pc()

    async def _write(self):
        with suppress(Exception):  # the browser left mid-send
            while True:
                await self.ws.send_json(await self.outbox.get())

    async def _results(self):
        events = stream.live.subscribe(replay_look=False)
        try:
            async for event in events:
                if event["type"] == "objects":
                    self.send(boxes(event))
                elif event["type"] == "status":
                    self.send(event)
        finally:
            await events.aclose()

    # ---------- signaling ----------

    async def _on_message(self, text: str):
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "offer" and isinstance(msg.get("sdp"), str):
            await self._answer(msg["sdp"])
        elif kind == "candidate":
            await self._candidate(msg)
        else:
            log.info("rtc_viewer: %s sent a message of type %.40r; ignored", self, kind)

    async def _answer(self, sdp: str):
        await self.close_pc()
        track = webrtc.phones.viewer_track()
        if track is None:
            self.send({"type": "error", "message": "no phone is streaming yet"})
            return
        pc = self.pc = RTCPeerConnection(webrtc.rtc_configuration(await webrtc.phones.ice.get()))
        pc.on("connectionstatechange", lambda: log.info("rtc_viewer: %s connection %s", self, pc.connectionState))
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            if not any(t.kind == "video" for t in pc.getTransceivers()):
                raise ValueError("the offer has no video transceiver")
            pc.addTrack(track)
            await pc.setLocalDescription(await pc.createAnswer())
        except Exception as e:
            track.stop()
            await self.close_pc()
            log.info("rtc_viewer: %s sent an unusable offer: %r", self, e)
            self.send({"type": "error", "message": f"unusable offer: {' '.join(str(e).split())[:200]}"})
            return
        log.info("rtc_viewer: %s answered: %s", self, webrtc.describe(pc.localDescription.sdp))
        self.send({"type": "answer", "sdp": pc.localDescription.sdp})

    async def _candidate(self, msg: dict):
        """A trickled ICE candidate from the browser; null (or "") is the end of them."""
        if self.pc is None or self.pc.remoteDescription is None:
            return  # one for a connection already closed
        raw = msg.get("candidate")
        try:
            if not raw:
                await self.pc.addIceCandidate(None)
                return
            candidate = candidate_from_sdp(raw.removeprefix("candidate:"))
            candidate.sdpMid, candidate.sdpMLineIndex = msg.get("sdpMid"), msg.get("sdpMLineIndex")
            await self.pc.addIceCandidate(candidate)
        except Exception as e:
            log.info("rtc_viewer: %s sent an unusable candidate: %r", self, e)

    async def close_pc(self):
        pc, self.pc = self.pc, None
        if pc:
            with suppress(Exception):
                await pc.close()


# ---------- routes ----------

router = APIRouter(prefix="/rtc")
viewers: set[Viewer] = set()
_count = 0


@router.get("/viewer", include_in_schema=False)
async def viewer_route():
    return FileResponse(stream.STATIC / "rtc_viewer.html")


@router.websocket("/ws")
async def ws_route(ws: WebSocket):
    global _count
    await ws.accept()
    if len(viewers) >= MAX_VIEWERS:
        await ws.close(code=1013, reason="too many viewers")  # 1013: try again later
        return
    _count += 1
    viewer = Viewer(ws, _count)
    viewers.add(viewer)
    log.info("rtc_viewer: %s connected (%d open)", viewer, len(viewers))
    try:
        await viewer.run()
    finally:
        viewers.discard(viewer)
        log.info("rtc_viewer: %s left (%d open)", viewer, len(viewers))


async def close():
    """Close every viewer's peer connection (for shutdown). Never raises."""
    await asyncio.gather(*(v.close_pc() for v in list(viewers)), return_exceptions=True)
