"""Tests for tracker.py and stream.py: mocked HTTP and a fake tracking server. No GPU, no network.

    python -m unittest test_stream -v                                  (from backend/)
    python test_stream.py --serve [--port 8003] [--latency 1.5]        # the fake server, for a manual run
    python test_stream.py --make-video test_media/synthetic_room.mp4   # a slow pan over fixtures/lamp_room.jpg
"""
import argparse
import asyncio
import io
import json
import logging
import math
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import av
import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

import config
import stream
import tracker
from models import BBox
from stream import FileSource, Stream, WebcamSource
from tracker import Tracker

logging.getLogger("gtl").setLevel(logging.CRITICAL)  # the failure tests log expected warnings

BASE = "http://sam.test/inference"
HEADERS = {"CF-Access-Client-Id": "test-id", "CF-Access-Client-Secret": "test-secret"}


def setUpModule():
    # Fake credentials, and no real server: nothing here may reach the shared GPU or print the token.
    for name, value in (("CF_ACCESS_CLIENT_ID", "test-id"), ("CF_ACCESS_CLIENT_SECRET", "test-secret"),
                        ("MODEL_SERVICE_URL", "")):
        patcher = mock.patch.object(config, name, value)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def image(width: int, height: int = 0) -> Image.Image:
    return Image.new("RGB", (width, height or width * 3 // 4), (90, 120, 150))


def make_video(path, seconds: float = 1.0, fps: int = 15, size: tuple[int, int] = (320, 240)) -> Path:
    """A slow pan and zoom over fixtures/lamp_room.jpg that loops seamlessly, encoded with PyAV."""
    room = Image.open(config.FIXTURES / "lamp_room.jpg").convert("RGB")
    W, H = size
    n = round(seconds * fps)
    with av.open(str(path), "w") as out:
        video = out.add_stream("h264", rate=fps)
        video.width, video.height, video.pix_fmt = W, H, "yuv420p"
        for i in range(n):
            k = (1 - math.cos(2 * math.pi * i / n)) / 2  # 0 -> 1 -> 0
            cw = room.width / (1.15 + 0.3 * k)
            ch = min(cw * H / W, room.height)
            x, y = (room.width - cw) * (0.15 + 0.7 * k), (room.height - ch) / 2
            frame = room.crop((round(x), round(y), round(x + cw), round(y + ch))).resize(size)
            out.mux(video.encode(av.VideoFrame.from_image(frame)))
        out.mux(video.encode())
    return Path(path)


# ---------- the fake tracking server ----------

FAKE_OBJECTS = [  # id, label, score, box (normalized x, y, w, h)
    (1, "sofa", 0.91, (0.08, 0.55, 0.45, 0.35)),
    (2, "lamp", 0.84, (0.62, 0.12, 0.12, 0.55)),
    (3, "table", 0.77, (0.40, 0.72, 0.25, 0.18)),
]


def moving_boxes(i: int, W: int, H: int) -> list[dict]:
    """The fake objects on frame i, drifting sideways; the table drops out now and then."""
    dx = 0.06 * math.sin(i / 3)
    return [{"id": oid, "score": score, "prompt": label,
             "box": [round((x + dx) * W, 1), round(y * H, 1), round((x + dx + w) * W, 1), round((y + h) * H, 1)]}
            for oid, label, score, (x, y, w, h) in FAKE_OBJECTS if not (oid == 3 and i % 8 >= 5)]


def fake_server(latency: float = 0.0, max_sessions: int = 4) -> FastAPI:
    """Baptiste's tracking API with moving boxes. app.state.stats counts overlapping frames among others."""
    app = FastAPI(title="fake tracking server")
    sessions: dict[str, dict] = {}
    stats = {"created": 0, "frames": 0, "overlaps": 0, "deleted": 0}
    app.state.sessions, app.state.stats = sessions, stats

    @app.get("/health")
    async def health():
        return {"status": "ok", "model": "fake", "device": "cpu", "sessions": len(sessions)}

    @app.post("/session/auto")
    async def create_auto(body: dict = Body(default={})):
        if not body.get("category"):  # like the real server since 18 Oct
            raise HTTPException(400, 'category is required, e.g. "electronics"')
        return await create({"prompt": ", ".join(o[1] for o in FAKE_OBJECTS)})

    @app.post("/session")
    async def create(body: dict = Body(default={})):
        if not body.get("prompt"):  # like the real server since 17 Oct
            raise HTTPException(400, 'prompt is required: "cup, laptop" or ["cup", "laptop"] (or use /session/auto)')
        if len(sessions) >= max_sessions:
            raise HTTPException(503, f"too many sessions (max {max_sessions}); delete one or wait for expiry")
        sid = uuid.uuid4().hex
        sessions[sid] = {"frame_idx": 0, "busy": False}
        stats["created"] += 1
        return {"session_id": sid, "prompt": body.get("prompt", [o[1] for o in FAKE_OBJECTS])}

    @app.post("/session/{sid}/frame")
    async def frame(sid: str, request: Request):
        s = sessions.get(sid)
        if s is None:
            raise HTTPException(404, "unknown or expired session")
        stats["overlaps"] += s["busy"]
        s["busy"] = True
        try:
            img = Image.open(io.BytesIO(await request.body()))
            await asyncio.sleep(latency)
            i, s["frame_idx"] = s["frame_idx"], s["frame_idx"] + 1
            stats["frames"] += 1
            return {"frame_idx": i, "width": img.width, "height": img.height,
                    "objects": moving_boxes(i, img.width, img.height)}
        finally:
            s["busy"] = False

    @app.delete("/session/{sid}")
    async def delete(sid: str):
        if sessions.pop(sid, None) is None:
            raise HTTPException(404, "unknown or expired session")
        stats["deleted"] += 1
        return {"deleted": sid}

    return app


# ---------- tracker, against httpx.MockTransport ----------

class TrackerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.replies: list = []  # what the next frames get: a status, an exception, or a dict reply
        self.sessions = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Baptiste's API: sessions s1, s2, ...; frames answered from self.replies, else one box."""
        self.requests.append(request)
        path = request.url.path.removeprefix("/inference")
        if request.method == "POST" and path in ("/session", "/session/auto"):
            body = json.loads(request.content or b"{}")
            if path == "/session" and not body.get("prompt"):
                return httpx.Response(400, json={"detail": 'prompt is required: "cup, laptop" or ["cup", "laptop"] '
                                                           "(or use /session/auto)"})
            if path == "/session/auto" and not body.get("category"):
                return httpx.Response(400, json={"detail": 'category is required, e.g. "electronics"'})
            self.sessions += 1
            return httpx.Response(200, json={"session_id": f"s{self.sessions}", "labels": ["sofa"]})
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": path.rsplit("/", 1)[1]})
        reply = self.replies.pop(0) if self.replies else 200
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            return httpx.Response(200, json=reply)
        if reply != 200:
            return httpx.Response(reply, json={"detail": "nope"})
        img = Image.open(io.BytesIO(request.content))
        return httpx.Response(200, json={
            "frame_idx": 0, "width": img.width, "height": img.height,
            "objects": [{"id": 7, "score": 0.9, "box": [0, 0, img.width / 2, img.height / 2], "prompt": "sofa"}]})

    def tracker(self, handler=None, base_url: str = BASE) -> Tracker:
        # follow_redirects=True like net.client(), so a request that forgets to turn it off would show.
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler or self.handler), follow_redirects=True)
        self.addAsyncCleanup(client.aclose)

        async def sleep(s):
            self.sleeps.append(s)
        return Tracker(base_url=base_url, client=client, sleep=sleep)

    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.path.removeprefix('/inference')}" for r in self.requests]

    async def test_start_creates_a_session(self):
        t = self.tracker()
        self.assertTrue(await t.start())
        self.assertEqual(t.session_id, "s1")
        self.assertEqual(str(self.requests[0].url), f"{BASE}/session")
        self.assertEqual(json.loads(self.requests[0].content), {"prompt": config.SAM_CATEGORIES})  # ours, as /detect
        self.assertTrue(await t.start())  # already has one
        self.assertEqual(self.calls(), ["POST /session"])

    async def test_no_categories_means_the_servers_own_list(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        self.addAsyncCleanup(client.aclose)
        t = Tracker(base_url=BASE, client=client, categories="")
        self.assertTrue(await t.start())
        self.assertEqual(self.calls(), ["POST /session/auto"])
        self.assertEqual(json.loads(self.requests[0].content), {"category": config.SAM_AUTO_CATEGORY})

    async def test_send_creates_the_session_if_needed(self):
        t = self.tracker()
        result = await t.send(image(320))
        self.assertEqual(self.calls(), ["POST /session", "POST /session/s1/frame"])
        self.assertEqual(result.objects[0].id, 7)
        self.assertEqual(self.requests[1].headers["content-type"], "image/jpeg")

    async def test_boxes_are_normalized_from_the_downscaled_jpeg(self):
        sent = []

        def handler(request):
            if request.url.path.endswith("/frame"):
                img = Image.open(io.BytesIO(request.content))
                sent.append((img.format, img.size))
                self.replies.append({"frame_idx": 3, "width": img.width, "height": img.height, "objects": [
                    {"id": 1, "score": 0.95, "box": [64, 36, 320, 180], "prompt": "sofa"},
                    {"id": 2, "score": 0.7, "box": [-10, -5, 700, 400], "prompt": None},  # off the edges
                    {"id": 3, "score": 0.65, "box": [700, 10, 800, 20], "prompt": "lamp"},  # wholly outside
                ]})
            return self.handler(request)

        t0 = time.time()
        result = await self.tracker(handler).send(image(1280, 720), captured_at=t0)
        self.assertEqual(sent, [("JPEG", (640, 360))])
        self.assertEqual((result.frame_idx, result.width, result.height, result.captured_at), (3, 640, 360, t0))
        self.assertEqual([(o.id, o.label, o.box) for o in result.objects], [
            (1, "sofa", BBox(x=0.1, y=0.1, w=0.4, h=0.4)),
            (2, "object", BBox(x=0.0, y=0.0, w=1.0, h=1.0)),
        ])
        self.assertGreaterEqual(result.latency_ms, 0)

    async def test_small_frames_are_not_upscaled(self):
        result = await self.tracker().send(image(320, 240))
        self.assertEqual((result.width, result.height), (320, 240))
        self.assertEqual(result.objects[0].box, BBox(x=0.0, y=0.0, w=0.5, h=0.5))

    async def test_404_starts_a_new_session_and_resends(self):
        t = self.tracker()
        self.replies = [404]
        result = await t.send(image(320))
        self.assertIsNotNone(result)
        self.assertEqual(self.calls(), ["POST /session", "POST /session/s1/frame",
                                        "POST /session", "POST /session/s2/frame"])
        self.assertEqual(self.requests[1].content, self.requests[3].content)  # the same frame, resent
        self.assertEqual(t.session_id, "s2")
        self.assertEqual(self.sleeps, [])

    async def test_404_twice_gives_up_on_that_frame(self):
        t = self.tracker()
        self.replies = [404, 404]
        self.assertIsNone(await t.send(image(320)))
        self.assertIsNone(t.session_id)
        self.assertEqual(self.sleeps, [1.0])
        self.assertIsNotNone(await t.send(image(320)))
        self.assertEqual(t.session_id, "s3")

    async def test_server_errors_back_off_and_recover(self):
        t = self.tracker()
        self.replies = [503, 502, 530, httpx.ConnectError("refused"), 503, 500, 200, 503]
        results = [await t.send(image(320)) for _ in self.replies[:]]
        self.assertEqual([r is not None for r in results], [False] * 6 + [True, False])
        self.assertEqual(self.sleeps, [1, 2, 4, 8, 10, 10, 1])  # reset by the success
        self.assertIn("503", t.error)

    async def test_connection_errors_creating_the_session_back_off(self):
        def handler(request):
            self.requests.append(request)
            raise httpx.ConnectError("refused")
        t = self.tracker(handler)
        self.assertFalse(await t.start())
        for _ in range(3):
            self.assertIsNone(await t.send(image(320)))
        self.assertEqual(self.sleeps, [2, 4, 8])  # start() counted as the first failure
        self.assertIn("ConnectError", t.error)

    async def test_auth_failures_log_once_and_retry_slowly_without_redirects(self):
        for status in (302, 401, 403):
            self.requests, self.sleeps = [], []

            def handler(request, status=status):
                self.requests.append(request)
                return httpx.Response(status, headers={"Location": "https://login.test/cdn-cgi/access/login"})
            t = self.tracker(handler)
            with self.assertLogs("gtl.tracker", logging.WARNING) as logs:
                self.assertFalse(await t.start())
                for _ in range(3):
                    self.assertIsNone(await t.send(image(320)))
            self.assertEqual(len(logs.records), 1, status)
            self.assertIn(f"rejected the request ({status})", logs.output[0])
            self.assertEqual(self.sleeps, [30, 30, 30])
            self.assertTrue(all(r.url.host == "sam.test" for r in self.requests))  # no redirect followed
            self.assertNotIn("test-secret", "".join(logs.output))

    async def test_no_url_means_no_requests(self):
        t = self.tracker(base_url="")
        self.assertIsNone(await t.send(image(320)))
        self.assertEqual((self.requests, self.sleeps), ([], [30]))
        self.assertIn("MODEL_SERVICE_URL", t.error)

    async def test_bad_reply_backs_off(self):
        t = self.tracker()
        self.replies = [{"unexpected": True}]
        self.assertIsNone(await t.send(image(320)))
        self.assertEqual(self.sleeps, [1])

    async def test_a_timed_out_frame_retires_its_session(self):
        async def handler(request):
            if request.url.path.endswith("/s1/frame"):
                self.requests.append(request)
                await asyncio.sleep(1)  # still "processing" when we give up
            return self.handler(request)
        t = self.tracker(handler)
        with mock.patch.object(tracker, "TIMEOUT_S", 0.05):
            self.assertIsNone(await t.send(image(320)))
            # A new frame must not go to s1, which may still be busy with the old one.
            self.assertIsNotNone(await t.send(image(320)))
        self.assertEqual(self.calls(), ["POST /session", "POST /session/s1/frame", "DELETE /session/s1",
                                        "POST /session", "POST /session/s2/frame"])
        self.assertEqual(self.sleeps, [1])

    async def test_stop_deletes_the_session(self):
        t = self.tracker()
        await t.start()
        await t.stop()
        await t.stop()  # nothing left to delete
        self.assertEqual(self.calls(), ["POST /session", "DELETE /session/s1"])
        self.assertIsNone(t.session_id)

    async def test_stop_never_raises(self):
        def handler(request):
            if request.method == "DELETE":
                raise httpx.ConnectError("refused")
            return self.handler(request)
        t = self.tracker(handler)
        await t.start()
        await t.stop()
        self.assertIsNone(t.session_id)

    async def test_headers_go_out_on_every_request(self):
        t = self.tracker()
        self.replies = [404]
        await t.start()
        await t.send(image(320))
        await t.stop()
        self.assertEqual(len(self.requests), 5)
        for r in self.requests:
            self.assertEqual({k: r.headers.get(k) for k in HEADERS}, HEADERS, r.url)


# ---------- stream ----------

class Gate:
    """A MockTransport handler that holds every frame until the test lets it through."""

    def __init__(self):
        self.arrived: asyncio.Queue = asyncio.Queue()  # the width of each frame as it arrives
        self.open = asyncio.Semaphore(0)
        self.in_flight = self.max_in_flight = self.frames = 0
        self.calls: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path.removeprefix('/inference')}")
        if request.method == "POST" and request.url.path.endswith("/session"):
            return httpx.Response(200, json={"session_id": "s1", "prompt": ["sofa"]})
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": "s1"})
        img = Image.open(io.BytesIO(request.content))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.arrived.put_nowait(img.width)
        try:
            await self.open.acquire()
        finally:
            self.in_flight -= 1
        idx, self.frames = self.frames, self.frames + 1
        return httpx.Response(200, json={
            "frame_idx": idx, "width": img.width, "height": img.height,
            "objects": [{"id": 4, "score": 0.88, "box": [0, 0, img.width / 4, img.height / 2], "prompt": "lamp"}]})


async def next_event(events, type_: str, timeout: float = 3.0) -> dict:
    async with asyncio.timeout(timeout):
        async for event in events:
            if event["type"] == type_:
                return event


class StreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gate = Gate()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.gate))
        self.stream = Stream(Tracker(base_url=BASE, client=self.client))
        self.events = self.stream.subscribe()

    async def asyncTearDown(self):
        for _ in range(10):
            self.gate.open.release()
        await self.stream.stop()
        await self.events.aclose()
        await self.client.aclose()

    async def test_only_the_newest_frame_is_sent_and_never_two_at_once(self):
        self.assertEqual(await next_event(self.events, "status"), {"type": "status", "tracking": "stopped"})
        await self.stream.start()
        self.assertEqual(await next_event(self.events, "status"), {"type": "status", "tracking": "starting"})
        self.stream.push_frame(image(101))
        self.assertEqual(await asyncio.wait_for(self.gate.arrived.get(), 2), 101)
        for w in (102, 103, 104):  # arrive while 101 is in flight
            self.stream.push_frame(image(w))
            await asyncio.sleep(0.02)
        self.assertTrue(self.gate.arrived.empty())
        self.gate.open.release()
        self.assertEqual(await asyncio.wait_for(self.gate.arrived.get(), 2), 104)  # 102 and 103 dropped
        self.gate.open.release()
        await next_event(self.events, "objects")
        event = await next_event(self.events, "objects")
        self.assertEqual(event["frame_idx"], 1)
        self.assertEqual(event["width"], 104)
        await asyncio.sleep(0.05)
        self.assertTrue(self.gate.arrived.empty())  # nothing new pushed: nothing sent
        self.assertEqual(self.gate.max_in_flight, 1)
        self.assertIs(self.stream.tracked_frame(), self.stream.latest_frame())

    async def test_objects_event_shape_and_latest_state(self):
        await self.stream.start()
        frame = image(400, 300)
        self.stream.push_frame(frame)
        self.gate.open.release()
        event = await next_event(self.events, "objects")
        self.assertEqual(set(event), {"type", "frame_idx", "width", "height", "age_ms", "objects"})
        self.assertGreaterEqual(event["age_ms"], 0)
        self.assertEqual(event["objects"], [
            {"id": 4, "label": "lamp", "score": 0.88, "box": {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.5}}])
        self.assertEqual(await next_event(self.events, "status"), {"type": "status", "tracking": "ok"})

        result = self.stream.latest_objects()
        self.assertEqual((result.frame_idx, result.objects[0].label), (0, "lamp"))
        self.assertIs(self.stream.tracked_frame(), frame)
        later = image(420)
        self.stream.push_frame(later)
        self.assertIs(self.stream.latest_frame(), later)
        self.assertIs(self.stream.tracked_frame(), frame)  # its boxes still describe the older frame

        # A new subscriber starts with the current state.
        fresh = self.stream.subscribe()
        self.assertEqual(await anext(fresh), {"type": "status", "tracking": "ok"})
        self.assertEqual((await anext(fresh))["objects"], event["objects"])
        await fresh.aclose()

    async def test_the_latest_look_is_replayed_to_new_subscribers(self):
        def look(stage, capture_id):
            return {"type": "look", "stage": stage, "capture_id": capture_id, "track_id": 4}
        sent = [look("detected", "c_a"), look("described", "c_a"), look("detected", "c_b"),
                look("results", "c_a"),  # the older Look finishing late
                look("results", "c_b")]
        await next_event(self.events, "status")  # subscribed from here on
        for event in sent:
            self.stream.publish(event)
        self.assertEqual([await anext(self.events) for _ in sent], sent)  # listening: everything, once

        def replayed(event):
            return {**event, "replay": True}
        fresh = self.stream.subscribe()
        self.assertEqual(await anext(fresh), {"type": "status", "tracking": "stopped"})
        self.assertEqual([await anext(fresh), await anext(fresh)],
                         [replayed(look("detected", "c_b")), replayed(look("results", "c_b"))])
        await fresh.aclose()
        self.assertNotIn("replay", self.stream._look[0])  # the marker is only on what's replayed

        self.stream.publish(look("nothing", "c_c"))  # a photo with nothing to buy is a new Look too
        fresh = self.stream.subscribe()
        self.assertEqual([await anext(fresh), await anext(fresh)],
                         [{"type": "status", "tracking": "stopped"}, replayed(look("nothing", "c_c"))])
        await fresh.aclose()

    async def test_stop_deletes_the_session_and_clears_the_boxes(self):
        await self.stream.start()
        self.stream.push_frame(image(300))
        self.gate.open.release()
        await next_event(self.events, "objects")
        await self.stream.stop()
        self.assertEqual(self.gate.calls[-1], "DELETE /session/s1")
        self.assertIsNone(self.stream.latest_objects())
        self.assertEqual(await next_event(self.events, "status"), {"type": "status", "tracking": "ok"})
        self.assertEqual(await next_event(self.events, "status"), {"type": "status", "tracking": "stopped"})
        self.assertFalse(self.stream.running)

    async def test_tracking_down_shows_offline_and_video_goes_on(self):
        async def down(request):
            raise httpx.ConnectError("refused")
        client = httpx.AsyncClient(transport=httpx.MockTransport(down))
        self.addAsyncCleanup(client.aclose)
        s = Stream(Tracker(base_url=BASE, client=client, sleep=lambda _: asyncio.sleep(0.01)))
        events = s.subscribe()
        await s.start()
        s.push_frame(image(300))
        for state in ("starting", "offline"):  # first read after start(): the state then
            self.assertEqual(await next_event(events, "status"), {"type": "status", "tracking": state})
        s.push_frame(image(310))
        video = s.mjpeg()
        self.assertIn(b"\xff\xd8", await anext(video))
        await video.aclose()
        await s.stop()
        await events.aclose()

    async def test_mjpeg_parts(self):
        self.stream.push_frame(image(1600, 1200))
        video = self.stream.mjpeg()
        part = await anext(video)
        head, _, rest = part.partition(b"\r\n\r\n")
        self.assertEqual(head.split(b"\r\n")[:2], [b"--frame", b"Content-Type: image/jpeg"])
        jpeg = rest[:-2]
        self.assertEqual(head.split(b"\r\n")[2], b"Content-Length: %d" % len(jpeg))
        self.assertEqual(Image.open(io.BytesIO(jpeg)).size, (1024, 768))
        self.stream.push_frame(image(800, 600))
        self.assertEqual(Image.open(io.BytesIO((await anext(video)).partition(b"\r\n\r\n")[2][:-2])).size, (800, 600))
        await video.aclose()


# ---------- frame sources ----------

class Collector:
    """Stands in for a Stream: records what a source pushes and when."""

    def __init__(self):
        self.frames: list[tuple[float, Image.Image]] = []

    def push_frame(self, img):
        self.frames.append((time.monotonic(), img))


class SourceTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = make_video(Path(cls.tmp.name) / "room.mp4", seconds=0.8, fps=15)  # 12 frames

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    async def test_file_plays_at_its_frame_rate_without_blocking_the_loop(self):
        out, gaps = Collector(), []

        async def ticker():
            while True:
                t = time.monotonic()
                await asyncio.sleep(0.005)
                gaps.append(time.monotonic() - t)
        tick = asyncio.create_task(ticker())
        t0 = time.monotonic()
        await FileSource(self.video, loop=False).run(out)
        tick.cancel()
        self.assertEqual(len(out.frames), 12)
        self.assertEqual(out.frames[0][1].size, (320, 240))
        self.assertEqual(out.frames[0][1].mode, "RGB")
        self.assertGreater(out.frames[-1][0] - out.frames[0][0], 0.6)  # 11 frame gaps at 15 fps: 0.73s
        self.assertLess(time.monotonic() - t0, 3)
        self.assertLess(max(gaps), 0.1)

    async def test_file_loops_until_cancelled(self):
        out = Collector()
        task = asyncio.create_task(FileSource(self.video).run(out))
        await asyncio.sleep(1.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertGreater(len(out.frames), 12)
        n = len(out.frames)
        await asyncio.sleep(0.2)
        self.assertLessEqual(len(out.frames), n + 1)  # the decode thread stops too

    async def test_a_bad_file_never_raises(self):
        out = Collector()
        await FileSource(Path(self.tmp.name) / "missing.mp4").run(out)
        not_video = Path(self.tmp.name) / "notes.mp4"
        not_video.write_text("not a video")
        await FileSource(not_video).run(out)
        self.assertEqual(out.frames, [])

    def test_source_from(self):
        self.assertIsInstance(stream.source_from("webcam"), WebcamSource)
        with mock.patch.object(stream, "MEDIA", Path(self.tmp.name)):
            self.assertEqual(stream.source_from(f"file:{self.video}").path, self.video.resolve())
            for spec in ("file:config.py", f"file:{self.tmp.name}/../config.py", f"file:{self.tmp.name}/nope.mp4",
                         "rtsp://cam", "file:"):
                with self.assertRaises(Exception, msg=spec) as cm:
                    stream.source_from(spec)
                self.assertEqual(cm.exception.status_code, 400)


# ---------- end to end: video file -> stream -> tracker -> fake server -> events ----------

class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def test_video_to_events_through_the_fake_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = make_video(Path(tmp) / "room.mp4", seconds=1.0, fps=30)
            server = fake_server(latency=0.08)
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server))
            s = Stream(Tracker(base_url="http://fake", client=client))
            pushed = 0
            push = s.push_frame

            def counting_push(img):
                nonlocal pushed
                pushed += 1
                push(img)
            s.push_frame = counting_push
            events = s.subscribe()
            await s.start(FileSource(video))
            got = []
            async with asyncio.timeout(10):
                while len(got) < 8:
                    got.append(await next_event(events, "objects"))
            self.assertEqual(len(server.state.sessions), 1)
            await s.stop()
            await events.aclose()
            await client.aclose()

        stats = server.state.stats
        self.assertEqual(stats["overlaps"], 0)
        self.assertEqual((stats["created"], stats["deleted"]), (1, 1))
        self.assertEqual(server.state.sessions, {})
        self.assertLess(stats["frames"], pushed / 2)  # most frames were skipped, not queued
        self.assertEqual([e["frame_idx"] for e in got], list(range(8)))
        labels = {o["id"]: o["label"] for e in got for o in e["objects"]}
        self.assertEqual(labels, {1: "sofa", 2: "lamp", 3: "table"})  # ids stable, labels keyed by id
        self.assertEqual(got[0]["width"], 320)
        sofa = [next(o["box"] for o in e["objects"] if o["id"] == 1) for e in got]
        self.assertGreater(len({b["x"] for b in sofa}), 1)  # it moves
        self.assertTrue(all(0 <= b["x"] and b["x"] + b["w"] <= 1 for b in sofa))


# ---------- routes ----------

class RouteTest(unittest.TestCase):
    def setUp(self):
        self.calls: list[str] = []

        def handler(request):
            self.calls.append(f"{request.method} {request.url.path.removeprefix('/inference')}")
            if request.method == "DELETE":
                return httpx.Response(200, json={"deleted": "s1"})
            if request.url.path.endswith("/frame"):
                return httpx.Response(200, json={"frame_idx": 0, "width": 320, "height": 240, "objects": []})
            return httpx.Response(200, json={"session_id": "s1", "prompt": ["sofa"]})
        self.live = Stream(Tracker(base_url=BASE, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
        patcher = mock.patch.object(stream, "live", self.live)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(stream.router)
        self.app = app

    def client(self, host: str = "127.0.0.1") -> TestClient:
        return TestClient(self.app, client=(host, 50000))

    def test_viewer_page(self):
        with self.client() as c:
            r = c.get("/stream/viewer")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        for needle in ('"video.mjpg', 'new URL("events"', 'id="look"'):
            self.assertIn(needle, r.text)

    def test_events_start_with_the_current_state(self):
        with self.client() as c, c.websocket_connect("/stream/events") as ws:
            self.assertEqual(ws.receive_json(), {"type": "status", "tracking": "stopped"})

    def test_start_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(stream, "MEDIA", Path(tmp)):
            video = make_video(Path(tmp) / "room.mp4", seconds=0.5)
            with self.client() as c, c.websocket_connect("/stream/events") as ws:
                self.assertEqual(ws.receive_json()["tracking"], "stopped")
                r = c.post("/stream/start", json={"source": f"file:{video}"})
                self.assertEqual((r.status_code, r.json()), (200, {"tracking": "starting"}))
                self.assertEqual(ws.receive_json(), {"type": "status", "tracking": "starting"})
                r = c.post("/stream/stop")
                self.assertEqual((r.status_code, r.json()), (200, {"tracking": "stopped"}))
                events = [ws.receive_json()]
                while events[-1] != {"type": "status", "tracking": "stopped"}:
                    events.append(ws.receive_json())
                self.assertTrue(all(e["type"] in ("objects", "status") for e in events))
        self.assertEqual(self.calls[0], "POST /session")
        self.assertEqual(self.calls[-1], "DELETE /session/s1")

    def test_start_rejects_other_files(self):
        with self.client() as c:
            for source in ("file:config.py", "file:/etc/hosts", "http://example.com/a.mp4"):
                self.assertEqual(c.post("/stream/start", json={"source": source}).status_code, 400, source)
        self.assertFalse(self.live.running)

    def test_test_routes_are_local_only(self):
        with self.client("203.0.113.9") as c:
            self.assertEqual(c.post("/stream/stop").status_code, 403)
        with self.client() as c:  # a tunnel connects from localhost but says who it forwards for
            self.assertEqual(c.post("/stream/stop", headers={"X-Forwarded-For": "203.0.113.9"}).status_code, 403)
            self.assertEqual(c.post("/stream/start", json={"source": "webcam"},
                                    headers={"X-Forwarded-For": "203.0.113.9"}).status_code, 403)
        self.assertFalse(self.live.running)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--serve", action="store_true", help="run the fake tracking server")
    p.add_argument("--port", type=int, default=8003)
    p.add_argument("--latency", type=float, default=1.5, help="seconds per frame")
    p.add_argument("--make-video", metavar="PATH", help="write a 20s synthetic room video")
    args, rest = p.parse_known_args()
    if args.serve:
        import uvicorn
        uvicorn.run(fake_server(args.latency), host="127.0.0.1", port=args.port)
    elif args.make_video:
        print(make_video(args.make_video, seconds=20, fps=30, size=(960, 720)))
    else:
        unittest.main(argv=[sys.argv[0], *rest])
