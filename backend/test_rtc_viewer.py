"""Tests for rtc_viewer.py: the phone streams in (test_webrtc's stand-in), a stand-in browser connects
over a real WebSocket, trickles its ICE candidates, and gets the video and the boxes back.

    python -m unittest test_rtc_viewer -v        (from backend/)
"""
import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
import uvicorn
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from fastapi import FastAPI

import rtc_viewer
import stream
import webrtc
from stream import Stream
from test_stream import fake_server, make_video
from test_webrtc import PHONE_SIZE, Camera, PhoneClient, StaticIce, setUpModule, until  # noqa: F401
from tracker import Tracker


class BrowserClient:
    """Does what rtc_viewer.html does: waits for "live", offers one recvonly video transceiver, and
    trickles its candidates after the offer instead of putting them in it."""

    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.pc: RTCPeerConnection | None = None
        self.messages: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None

    async def open(self):
        self.ws = await websockets.connect(self.url)
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        async for text in self.ws:
            self.messages.put_nowait(json.loads(text))

    async def next(self, kind: str, timeout: float = 15.0) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                if (msg := await self.messages.get())["type"] == kind:
                    return msg

    async def send(self, msg: dict):
        await self.ws.send(json.dumps(msg))

    async def offer(self):
        if self.pc:
            await self.pc.close()
        self.pc = RTCPeerConnection(webrtc.rtc_configuration([]))
        self.pc.addTransceiver("video", direction="recvonly")
        await self.pc.setLocalDescription(await self.pc.createOffer())
        lines = self.pc.localDescription.sdp.splitlines()
        await self.send({"type": "offer", "sdp": "\r\n".join(
            line for line in lines if not line.startswith(("a=candidate:", "a=end-of-candidates"))) + "\r\n"})
        candidates = [line.removeprefix("a=") for line in lines if line.startswith("a=candidate:")]
        self.assertion_candidates = len(candidates)
        for candidate in candidates:
            await self.send({"type": "candidate", "candidate": candidate, "sdpMid": "0", "sdpMLineIndex": 0})
        await self.send({"type": "candidate", "candidate": None, "sdpMid": None, "sdpMLineIndex": None})

    async def connect(self):
        """Offer, apply the answer, and return the video track once connected."""
        await self.offer()
        answer = await self.next("answer")
        tracks: asyncio.Queue = asyncio.Queue()
        self.pc.on("track", tracks.put_nowait)
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
        return await asyncio.wait_for(tracks.get(), 10)

    async def close(self):
        if self.pc:
            await self.pc.close()
        if self.ws:
            await self.ws.close()
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MessageTest(unittest.TestCase):
    def test_boxes_lists_only_what_is_in_view(self):
        event = {"type": "objects", "frame_idx": 7, "width": 504, "height": 896, "age_ms": 120, "objects": [
            {"id": 3, "label": "chair", "score": 0.88, "box": {"x": 0.31, "y": 0.22, "w": 0.27, "h": 0.49},
             "in_view": True},
            {"id": 4, "label": "lamp", "score": 0.7, "box": {"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1},
             "in_view": False}]}
        self.assertEqual(rtc_viewer.boxes(event), {
            "type": "boxes", "width": 504, "height": 896, "age_ms": 120,
            "objects": [{"track_id": "t3", "label": "chair", "score": 0.88, "box": [0.31, 0.22, 0.58, 0.71]}]})


class ViewerTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = make_video(Path(cls.tmp.name) / "room.mp4", seconds=1.0, fps=30, size=(480, 360))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    async def asyncSetUp(self):
        self.sam = fake_server(latency=0.05)
        self.sam_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.sam))
        self.live = Stream(Tracker(base_url="http://fake", client=self.sam_client))
        self.phones = webrtc.Phones(StaticIce([]))  # host candidates only: no network
        for target, name, value in ((stream, "live", self.live), (webrtc, "phones", self.phones),
                                    (rtc_viewer, "viewers", set())):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(webrtc.router)
        app.include_router(rtc_viewer.router)
        port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="off"))
        self.serving = asyncio.create_task(self.server.serve())
        await until(lambda: self.server.started)
        self.base = f"http://127.0.0.1:{port}"
        self.http = httpx.AsyncClient(base_url=self.base)
        self.closers = []

    async def asyncTearDown(self):
        for close in reversed(self.closers):
            await close()
        await rtc_viewer.close()
        await self.phones.close()
        await self.live.stop()
        self.server.should_exit = True
        await self.serving
        await self.http.aclose()
        await self.sam_client.aclose()

    async def phone(self) -> PhoneClient:
        client = PhoneClient(self.http, Camera(self.video))
        self.closers.append(client.close)
        await client.connect()
        return client

    async def browser(self) -> BrowserClient:
        client = BrowserClient(self.base.replace("http", "ws") + "/rtc/ws")
        self.closers.append(client.close)
        await client.open()
        return client

    async def test_the_page_is_served(self):
        r = await self.http.get("/rtc/viewer")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/rtc/ws", r.text)

    async def test_video_and_boxes_reach_the_browser(self):
        phone = await self.phone()
        await phone.detections()
        browser = await self.browser()
        hello = await browser.next("hello")
        self.assertEqual(hello, {"type": "hello", "iceServers": [], "rotate": 0, "live": True})
        track = await browser.connect()
        self.assertGreater(browser.assertion_candidates, 0)  # they came trickled, not in the offer
        frame = await asyncio.wait_for(track.recv(), 10)
        self.assertEqual((frame.width, frame.height), PHONE_SIZE)
        await until(lambda: browser.pc.connectionState == "connected")
        msg = await browser.next("boxes")
        self.assertEqual((msg["width"], msg["height"]), PHONE_SIZE)
        self.assertTrue(msg["objects"])
        for o in msg["objects"]:
            self.assertEqual(set(o), {"track_id", "label", "score", "box"})
            x1, y1, x2, y2 = o["box"]
            self.assertTrue(0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1, o)
        await phone.detections()  # the phone's own stream goes on beside the viewer's

    async def test_waits_for_a_phone_then_follows_it(self):
        browser = await self.browser()
        self.assertFalse((await browser.next("hello"))["live"])
        await browser.offer()
        self.assertEqual(await browser.next("error"), {"type": "error", "message": "no phone is streaming yet"})
        phone = await self.phone()
        self.assertEqual(await browser.next("live"), {"type": "live", "live": True})
        track = await browser.connect()
        await asyncio.wait_for(track.recv(), 10)
        await phone.close()
        self.assertEqual(await browser.next("live"), {"type": "live", "live": False})
        await until(lambda: browser.pc.connectionState == "closed")

    async def test_junk_is_ignored(self):
        await self.phone()
        browser = await self.browser()
        await browser.next("hello")
        for junk in ("not json", "[1]", '{"type": "hello"}', '{"type": "offer"}',
                     '{"type": "candidate", "candidate": "garbage", "sdpMid": "0"}'):
            await browser.ws.send(junk)
        await browser.connect()  # still works

    async def test_too_many_viewers(self):
        with mock.patch.object(rtc_viewer, "MAX_VIEWERS", 1):
            first = await self.browser()
            await first.next("hello")
            second = await self.browser()
            await asyncio.gather(asyncio.wait_for(second._reader, 5), return_exceptions=True)
            self.assertEqual(second.ws.close_code, 1013)


if __name__ == "__main__":
    unittest.main()
