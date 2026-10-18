"""Tests for tracker.py and stream.py: mocked HTTP and a fake tracking server. No GPU, no network."""
import asyncio
import io
import json
import logging
import time
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from fakes.frames import image
from fakes.tracking import fake_server
from storefront import config
from storefront.api import viewer
from storefront.live import stream, tracker
from storefront.live.stream import Stream
from storefront.live.tracker import Tracker
from storefront.models import BBox

BASE = "http://sam.test/inference"
HEADERS = {"CF-Access-Client-Id": "test-id", "CF-Access-Client-Secret": "test-secret"}


def setUpModule():
    # Fake credentials, and no real server: nothing here may reach the shared GPU or print the token.
    for name, value in (("CF_ACCESS_CLIENT_ID", "test-id"), ("CF_ACCESS_CLIENT_SECRET", "test-secret"),
                        ("MODEL_SERVICE_URL", "")):
        patcher = mock.patch.object(config, name, value)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


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
        if request.method == "POST" and path == "/session":
            body = json.loads(request.content or b"{}")
            if not body.get("prompt"):
                return httpx.Response(400, json={"detail": 'prompt is required: "cup, laptop" or ["cup", "laptop"]'})
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
        self.assertEqual(json.loads(self.requests[0].content), {"prompt": config.SAM_CATEGORIES})
        self.assertTrue(await t.start())  # already has one
        self.assertEqual(self.calls(), ["POST /session"])

    def auto_tracker(self, picked=("lamp", "rug")) -> tuple[Tracker, list]:
        """A tracker without categories, with Claude's labels faked; picks records each call, in order
        with the server's requests."""
        picks = []

        async def fake_labels(image, category):
            picks.append((image.size, category, len(self.requests)))  # how many requests went before
            return list(picked)
        patcher = mock.patch("storefront.live.labels.labels_or_fallback", fake_labels)
        patcher.start()
        self.addCleanup(patcher.stop)
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        self.addAsyncCleanup(client.aclose)
        return Tracker(base_url=BASE, client=client, categories=""), picks

    async def test_no_categories_waits_for_claudes_labels_from_the_first_frame(self):
        t, picks = self.auto_tracker()
        self.assertTrue(await t.start())
        self.assertEqual((self.calls(), picks), ([], []))  # no frame yet: nothing to pick labels from
        result = await t.send(image(320))
        self.assertEqual(picks, [((320, 240), config.SAM_AUTO_CATEGORY, 0)])  # before any request
        self.assertEqual(self.calls(), ["POST /session", "POST /session/s1/frame"])
        self.assertEqual(json.loads(self.requests[0].content), {"prompt": ["lamp", "rug"]})
        self.assertEqual(result.objects[0].id, 7)
        await t.send(image(320))  # same session: no new labels
        self.assertEqual(len(picks), 1)

    async def test_no_categories_picks_new_labels_for_a_new_session(self):
        t, picks = self.auto_tracker()
        await t.send(image(320))
        self.replies = [404]  # expired
        await t.send(image(320))
        self.assertEqual(self.calls(), ["POST /session", "POST /session/s1/frame", "POST /session/s1/frame",
                                        "POST /session", "POST /session/s2/frame"])
        self.assertEqual([p[2] for p in picks], [0, 3])  # each right before its session

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
            with self.assertLogs("storefront.tracker", logging.WARNING) as logs:
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
        [lamp] = [dict(o) for o in event["objects"]]
        seen = lamp.pop("first_seen_ts")
        self.assertEqual(lamp.pop("last_seen_ts"), seen)  # a first sighting
        self.assertLess(abs(seen - time.time() * 1000), 5000)
        self.assertEqual(lamp, {"id": 4, "label": "lamp", "score": 0.88, "box": {"x": 0.0, "y": 0.0, "w": 0.25, "h": 0.5},
                                "in_view": True, "centered": 0.36, "seconds_viewed": 0.0})  # scene memory's fields
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

    async def test_a_reset_forgets_everything_and_drops_answers_from_before(self):
        await self.stream.start()
        self.stream.push_frame(image(300))
        self.assertEqual(await asyncio.wait_for(self.gate.arrived.get(), 2), 300)
        self.gate.open.release()
        await next_event(self.events, "objects")
        self.assertEqual(set(self.stream.scene.tracks), {4})
        self.stream.push_frame(image(310))  # the old camera's last frame, in flight
        self.assertEqual(await asyncio.wait_for(self.gate.arrived.get(), 2), 310)

        resetting = asyncio.create_task(self.stream.reset())
        cleared = await next_event(self.events, "objects")
        self.assertEqual(cleared["objects"], [])  # at once: subscribers drop what they had
        self.assertEqual((self.stream.scene.tracks, self.stream.latest_frame()), ({}, None))
        await asyncio.sleep(0.05)
        self.assertFalse(resetting.done())  # the session ends once the frame in flight is answered
        self.gate.open.release()
        await asyncio.wait_for(resetting, 2)
        self.assertEqual(self.gate.calls[-1], "DELETE /session/s1")
        self.assertEqual(self.stream.scene.tracks, {})  # that answer came back after the reset: dropped

        self.stream.push_frame(image(320))  # the new camera
        self.assertEqual(await asyncio.wait_for(self.gate.arrived.get(), 2), 320)
        self.gate.open.release()
        event = await next_event(self.events, "objects")
        self.assertEqual([o["id"] for o in event["objects"]], [4])
        self.assertEqual(self.gate.calls[-2:], ["POST /session", "POST /session/s1/frame"])  # a new session

    async def test_stop_purges_what_was_tracked(self):
        await self.stream.start()
        self.stream.push_frame(image(300))
        self.gate.open.release()
        await next_event(self.events, "objects")
        await self.stream.stop()
        self.assertEqual((await next_event(self.events, "objects"))["objects"], [])
        self.assertEqual((self.stream.scene.tracks, self.stream.latest_frame(), self.stream.tracked_frame()),
                         ({}, None, None))

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


# ---------- end to end: frames -> stream -> tracker -> fake server -> events ----------

class EndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def test_frames_to_events_through_the_fake_server(self):
        server = fake_server(latency=0.08)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server))
        s = Stream(Tracker(base_url="http://fake", client=client))
        pushed = 0

        async def camera():  # 30 fps, like the phone
            nonlocal pushed
            while True:
                s.push_frame(image(320, 240))
                pushed += 1
                await asyncio.sleep(1 / 30)
        events = s.subscribe()
        await s.start()
        frames = asyncio.create_task(camera())
        got = []
        async with asyncio.timeout(10):
            while len(got) < 8:
                got.append(await next_event(events, "objects"))
        self.assertEqual(len(server.state.sessions), 1)
        frames.cancel()
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
        app.include_router(viewer.router)
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
