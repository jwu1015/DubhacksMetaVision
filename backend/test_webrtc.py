"""Tests for webrtc.py, and a stand-in for the iPhone app. No GPU, no Cloudflare: both peers run in
this process and connect over localhost; tracking is test_stream's fake server, Cloudflare is mocked.

    python -m unittest test_webrtc -v                                   (from backend/)
    python test_webrtc.py --client [--url http://127.0.0.1:8004] [--video test_media/synthetic_room.mp4]
                                   [--seconds 30] [--codec H264|VP8]    # play the phone against a server
"""
import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import av
import httpx
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from aiortc.mediastreams import MediaStreamError
from fastapi import FastAPI

import config
import stream
import webrtc
from stream import Stream
from test_stream import fake_server, make_video  # also quiets the "gtl" logs, as in test_stream
from tracker import Tracker

PHONE_SIZE = (504, 896)  # the glasses' portrait frames, as the app sends them
PHONE_FPS = 15


def setUpModule():
    # Nothing here may reach Cloudflare, the shared GPU, or a TURN server set in .env.
    for target, name, value in ((config, "CF_ACCESS_CLIENT_ID", ""), (config, "MODEL_SERVICE_URL", ""),
                                (webrtc, "CF_TURN_KEY_ID", ""), (webrtc, "CF_TURN_KEY_API_TOKEN", ""),
                                (webrtc, "TURN_URLS", []), (webrtc, "ROTATE", 0)):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


# ---------- the phone ----------

class Camera(MediaStreamTrack):
    """The phone's camera: a video file center-cropped to the glasses' portrait 504x896 at 15 fps,
    played again whenever it ends. Retimed here: aiortc's MediaPlayer stops pacing after its first loop."""
    kind = "video"

    def __init__(self, path, size: tuple[int, int] = PHONE_SIZE, fps: int = PHONE_FPS):
        super().__init__()
        self.path, self.size, self.fps = str(path), size, fps
        self._player: MediaPlayer | None = None
        self._next = 0.0  # file time of the next frame to keep
        self._n = 0

    async def recv(self) -> av.VideoFrame:
        while True:
            if self._player is None:
                self._player, self._next = MediaPlayer(self.path), 0.0  # paces itself at the file's rate
            try:
                frame = await self._player.video.recv()
            except MediaStreamError:
                self._player = None
                continue
            if frame.time is None or frame.time + 1e-3 >= self._next:
                break
        self._next += 1 / self.fps
        img = frame.to_image()
        (W, H), (w, h) = img.size, self.size
        if W * h > H * w:  # wider than the phone's frame: crop the sides
            cw = round(H * w / h)
            img = img.crop(((W - cw) // 2, 0, (W - cw) // 2 + cw, H))
        else:
            ch = round(W * h / w)
            img = img.crop((0, (H - ch) // 2, W, (H - ch) // 2 + ch))
        out = av.VideoFrame.from_image(img.resize(self.size))
        out.pts, out.time_base = self._n * (90000 // self.fps), Fraction(1, 90000)
        self._n += 1
        return out

    def stop(self):
        super().stop()
        if self._player:
            self._player.video.stop()


class PhoneClient:
    """Does what the iPhone app does: GET /ice-servers, then an offer with one sendonly video track
    (H.264 first, VP8 as fallback) and a "detections" channel it creates, POST /offer, then detections
    in and selects out."""

    def __init__(self, http: httpx.AsyncClient, track: MediaStreamTrack, codec: str = "H264", use_ice: bool = True):
        self.http, self.track, self.codec, self.use_ice = http, track, codec, use_ice
        self.ice_servers: list[dict] = []
        self.pc: RTCPeerConnection | None = None
        self.channel = None
        self.messages: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        r = await self.http.get("/ice-servers")
        r.raise_for_status()
        self.ice_servers = r.json()["iceServers"]
        self.pc = RTCPeerConnection(webrtc.rtc_configuration(self.ice_servers if self.use_ice else []))
        video = self.pc.addTransceiver(self.track, direction="sendonly")
        order = {"video/H264": 1, "video/VP8": 1, f"video/{self.codec}": 0}
        video.setCodecPreferences(sorted(RTCRtpSender.getCapabilities("video").codecs,
                                         key=lambda c: order.get(c.mimeType, 2)))  # rtx last
        self.channel = self.pc.createDataChannel("detections")
        self.channel.on("message", lambda m: self.messages.put_nowait(json.loads(m)))
        await self.pc.setLocalDescription(await self.pc.createOffer())
        r = await self.http.post("/offer", json={"sdp": self.pc.localDescription.sdp, "type": "offer"})
        r.raise_for_status()
        answer = r.json()
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

    async def detections(self, timeout: float = 15.0) -> dict:
        """The next detections message with at least one object."""
        async with asyncio.timeout(timeout):
            while True:
                msg = await self.messages.get()
                if msg.get("type") == "detections" and msg["objects"]:
                    return msg

    def select(self, track_id: str):
        self.channel.send(json.dumps({"type": "select", "track_id": track_id}))

    async def close(self):
        if self.pc:
            await self.pc.close()
        self.track.stop()


class StaticIce:
    """Stands in for webrtc.IceServers."""
    mode = "test"

    def __init__(self, servers: list[dict]):
        self.servers = servers

    async def get(self) -> list[dict]:
        return self.servers


async def until(condition, timeout: float = 10.0):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.05)


# ---------- ICE servers, against httpx.MockTransport ----------

MINTED = {"iceServers": [  # Cloudflare's documented reply, plus the alternate port 53 it may add
    {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turn:turn.cloudflare.com:53?transport=udp",
              "turn:turn.cloudflare.com:443?transport=udp", "turns:turn.cloudflare.com:443?transport=tcp"],
     "username": "minted-user-1234", "credential": "minted-secret-5678"}]}
USABLE = [
    {"urls": ["stun:stun.cloudflare.com:3478"]},
    {"urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turn:turn.cloudflare.com:443?transport=udp",
              "turns:turn.cloudflare.com:443?transport=tcp"],
     "username": "minted-user-1234", "credential": "minted-secret-5678"}]
SECRETS = ("cf-api-token-secret", "minted-user-1234", "minted-secret-5678")


class IceServersTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests: list[httpx.Request] = []
        self.replies: list = []  # what the next mints get: a status, an exception, or a dict reply
        self.now = 1_000_000.0
        for name, value in (("CF_TURN_KEY_ID", "cf-key-id"), ("CF_TURN_KEY_API_TOKEN", "cf-api-token-secret"),
                            ("TURN_URLS", []), ("TURN_USERNAME", ""), ("TURN_CREDENTIAL", "")):
            patcher = mock.patch.object(webrtc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else MINTED
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            return httpx.Response(201, json=reply)
        return httpx.Response(reply, json={"errors": [{"message": "nope"}]}, headers={"Location": "https://x.test/"})

    def ice(self, handler=None) -> webrtc.IceServers:
        # follow_redirects=True like net.client(), so a request that forgets to turn it off would show.
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler or self.handler), follow_redirects=True)
        self.addAsyncCleanup(client.aclose)
        return webrtc.IceServers(client=client, clock=lambda: self.now)

    async def test_mints_with_the_turn_key_and_drops_port_53(self):
        servers = await self.ice().get()
        self.assertEqual(servers, USABLE)
        (r,) = self.requests
        self.assertEqual((r.method, str(r.url)), (
            "POST", "https://rtc.live.cloudflare.com/v1/turn/keys/cf-key-id/credentials/generate-ice-servers"))
        self.assertEqual(r.headers["authorization"], "Bearer cf-api-token-secret")
        self.assertEqual(r.headers["content-type"], "application/json")
        self.assertEqual(json.loads(r.content), {"ttl": 3 * 3600})

    async def test_cached_until_an_hour_is_left(self):
        ice = self.ice()
        await ice.get()
        self.now += 2 * 3600 - 1  # minted for 3h: an hour and a second left
        self.assertEqual(await ice.get(), USABLE)
        self.assertEqual(len(self.requests), 1)
        self.now += 2
        self.replies = [{"iceServers": [{**USABLE[1], "username": "fresh-user"}]}]
        self.assertEqual((await ice.get())[0]["username"], "fresh-user")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(await ice.get()), 1)  # and that one is cached
        self.assertEqual(len(self.requests), 2)

    async def test_requests_that_arrive_together_share_one_mint(self):
        async def slow(request):
            await asyncio.sleep(0.05)
            return await self.handler(request)
        ice = self.ice(slow)
        results = await asyncio.gather(*(ice.get() for _ in range(5)))
        self.assertEqual(results, [USABLE] * 5)
        self.assertEqual(len(self.requests), 1)

    async def test_a_failed_mint_falls_back_to_stun_and_retries_later(self):
        failures = [401, 403, 500, 302, httpx.ConnectError("refused"), {"unexpected": True},
                    {"iceServers": [{"urls": ["stun:stun.cloudflare.com:3478"]}]}, "timeout"]
        for failure in failures:
            with self.subTest(failure=failure):
                self.requests = []

                async def handler(request, failure=failure):
                    if failure == "timeout":
                        self.requests.append(request)
                        await asyncio.sleep(1)
                    self.replies = [failure]
                    return await self.handler(request)
                ice = self.ice(handler)
                with mock.patch.object(webrtc, "MINT_TIMEOUT_S", 0.05), \
                        self.assertLogs("gtl.webrtc", logging.WARNING) as logs:
                    self.assertEqual(await ice.get(), [webrtc.STUN])
                    self.now += 29
                    self.assertEqual(await ice.get(), [webrtc.STUN])  # no new mint for 30s
                self.assertEqual(len(self.requests), 1)
                self.assertIn("minting TURN credentials failed", logs.output[0])
                self.assertIn("STUN only", logs.output[1])
                self.assertEqual(len(logs.records), 2)  # the STUN warning once
                self.assertTrue(all(r.url.host == "rtc.live.cloudflare.com" for r in self.requests))
                self.now += 2
                self.replies = []
                self.assertEqual(await self.ice().get(), USABLE)  # a new instance: no backoff

    async def test_a_failed_refresh_keeps_the_old_credential_while_it_lasts(self):
        ice = self.ice()
        await ice.get()
        self.now += 2.5 * 3600  # half an hour left
        self.replies = [503]
        with self.assertLogs("gtl.webrtc", logging.WARNING):
            self.assertEqual(await ice.get(), USABLE)
        self.now += 0.5 * 3600 + 1  # expired, and Cloudflare still down
        self.replies = [503]
        with self.assertLogs("gtl.webrtc", logging.WARNING):
            self.assertEqual(await ice.get(), [webrtc.STUN])
        self.assertEqual(len(self.requests), 3)

    async def test_credentials_and_token_are_never_logged(self):
        async def run():
            ice = self.ice()
            await ice.get()
            self.now += 3 * 3600
            self.replies = [500]
            await ice.get()
            self.replies = [httpx.ConnectError("refused cf-api-token-secret")]
            self.now += 60
            await ice.get()
        with self.assertLogs("gtl", logging.DEBUG) as ours, self.assertLogs("httpx", logging.DEBUG) as httpx_logs:
            await run()
        text = "\n".join(ours.output + httpx_logs.output)
        self.assertIn("minted in", text)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    async def test_static_turn_settings(self):
        with mock.patch.multiple(webrtc, CF_TURN_KEY_ID="", TURN_USERNAME="static-user", TURN_CREDENTIAL="static-pw",
                                 TURN_URLS=["turn:turn.example:3478?transport=udp", "turn:turn.example:oops",
                                            "turn:turn.example:53?transport=udp"]):
            ice = self.ice()
            with self.assertLogs("gtl.webrtc", logging.WARNING) as logs:
                servers = await ice.get()
                await ice.get()
        self.assertEqual(servers, [webrtc.STUN, {"urls": ["turn:turn.example:3478?transport=udp"],
                                                 "username": "static-user", "credential": "static-pw"}])
        self.assertEqual(len(logs.records), 1)  # the malformed URL, once
        self.assertIn("turn:turn.example:oops", logs.output[0])
        self.assertEqual(self.requests, [])

    async def test_nothing_configured_is_stun_only_with_one_warning(self):
        with mock.patch.multiple(webrtc, CF_TURN_KEY_ID="", CF_TURN_KEY_API_TOKEN=""):
            ice = self.ice()
            with self.assertLogs("gtl.webrtc", logging.WARNING) as logs:
                self.assertEqual(await ice.get(), [{"urls": ["stun:stun.cloudflare.com:3478"]}])
                self.assertEqual(await ice.get(), [webrtc.STUN])
            self.assertEqual(ice.mode, "STUN only")
        self.assertEqual(len(logs.records), 1)
        self.assertIn("set CF_TURN_KEY_ID and CF_TURN_KEY_API_TOKEN", logs.output[0])
        self.assertEqual(self.requests, [])

    def test_rtc_configuration(self):
        config_ = webrtc.rtc_configuration(USABLE)
        self.assertEqual([(s.urls, s.username, s.credential) for s in config_.iceServers], [
            (USABLE[0]["urls"], None, None), (USABLE[1]["urls"], "minted-user-1234", "minted-secret-5678")])
        self.assertEqual(webrtc.rtc_configuration([]).iceServers, [])  # not None: no Google STUN added


# ---------- channel messages ----------

class MessageTest(unittest.TestCase):
    def test_corners(self):
        self.assertEqual(webrtc.corners({"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}), [0.1, 0.2, 0.4, 0.6])
        self.assertEqual(webrtc.corners({"x": 0.6667, "y": 0.0, "w": 0.3334, "h": 1.0}), [0.6667, 0.0, 1.0, 1.0])

    def test_detections_message(self):
        event = {"type": "objects", "frame_idx": 7, "width": 504, "height": 896, "age_ms": 1200, "objects": [
            {"id": 3, "label": "chair", "score": 0.88, "box": {"x": 0.31, "y": 0.22, "w": 0.27, "h": 0.49}},
            {"id": 12, "label": "lamp", "score": 0.74, "box": {"x": 0.7, "y": 0.3, "w": 0.28, "h": 0.3}}]}
        before = time.time() * 1000
        msg = webrtc.detections(event)
        self.assertLessEqual(before - 1, msg.pop("ts"), time.time() * 1000 + 1)
        self.assertEqual(msg, {"type": "detections", "objects": [
            {"track_id": "t3", "box": [0.31, 0.22, 0.58, 0.71], "label": "chair", "score": 0.88},
            {"track_id": "t12", "box": [0.7, 0.3, 0.98, 0.6], "label": "lamp", "score": 0.74}]})
        self.assertEqual(webrtc.detections({**event, "objects": []})["objects"], [])

    def test_track_number(self):
        for track_id, number in (("t3", 3), ("t12", 12), ("t0", 0)):
            self.assertEqual(webrtc.track_number(track_id), number)
        for track_id in ("3", "t", "tx", "t-1", "t1.5", "t²", "T3", 3, None, ["t3"]):
            self.assertIsNone(webrtc.track_number(track_id), track_id)

    def test_describe(self):
        self.assertEqual(webrtc.describe("not sdp"), "no media")


# ---------- end to end: phone -> WebRTC -> stream -> fake tracking server -> detections -> phone ----------

class PhoneTest(unittest.IsolatedAsyncioTestCase):
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
        self.pushed: list[tuple[int, int]] = []
        push = self.live.push_frame

        def counting_push(img):
            self.pushed.append(img.size)
            push(img)
        self.live.push_frame = counting_push
        self.phones = webrtc.Phones(StaticIce([]))  # host candidates only: no network
        for target, name, value in ((stream, "live", self.live), (webrtc, "phones", self.phones),
                                    (webrtc, "select_handler", None)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(stream.router)
        app.include_router(webrtc.router)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend")
        self.clients: list[PhoneClient] = []
        self.events: list[dict] = []  # every "objects" event, to check the detections against

        async def collect(events):
            async for event in events:
                if event["type"] == "objects":
                    self.events.append(event)
        self.collector = asyncio.create_task(collect(self.live.subscribe()))

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await self.phones.close()
        await self.live.stop()
        self.collector.cancel()
        await asyncio.gather(self.collector, return_exceptions=True)
        await self.http.aclose()
        await self.sam_client.aclose()

    async def phone(self, **kw) -> PhoneClient:
        client = PhoneClient(self.http, Camera(self.video), **kw)
        self.clients.append(client)
        await client.connect()
        return client

    def stats(self) -> dict:
        return {**self.sam.state.stats, "open": len(self.sam.state.sessions)}

    async def test_video_in_detections_out(self):
        phone = await self.phone()
        self.assertIn("video recvonly H264, data channel", webrtc.describe(phone.pc.remoteDescription.sdp))
        msg = await phone.detections()
        self.assertEqual(set(msg), {"type", "ts", "objects"})
        self.assertEqual(msg["type"], "detections")
        self.assertIsInstance(msg["ts"], int)
        self.assertLess(abs(msg["ts"] - time.time() * 1000), 5000)
        for o in msg["objects"]:
            self.assertEqual(set(o), {"track_id", "box", "label", "score"})
            self.assertEqual(o["label"], {"t1": "sofa", "t2": "lamp", "t3": "table"}[o["track_id"]])
            x1, y1, x2, y2 = o["box"]
            self.assertTrue(0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1, o)

        # Exactly the boxes of an "objects" event, as corners.
        def as_corners(event):
            return [{"track_id": f"t{o['id']}", "label": o["label"], "score": o["score"],
                     "box": [o["box"]["x"], o["box"]["y"], round(o["box"]["x"] + o["box"]["w"], 4),
                             round(o["box"]["y"] + o["box"]["h"], 4)]} for o in event["objects"]]
        self.assertIn(msg["objects"], [as_corners(e) for e in self.events])

        # The phone's frames reached the stream as sent, portrait and upright, and went on to tracking.
        self.assertEqual(set(self.pushed), {PHONE_SIZE})
        self.assertEqual(self.live.latest_frame().size, PHONE_SIZE)
        self.assertEqual({(e["width"], e["height"]) for e in self.events}, {PHONE_SIZE})
        for _ in range(3):
            await phone.detections()  # and they keep coming
        self.assertEqual(self.stats()["overlaps"], 0)

    async def test_vp8_works_too(self):
        phone = await self.phone(codec="VP8")
        self.assertIn("video recvonly VP8", webrtc.describe(phone.pc.remoteDescription.sdp))
        await phone.detections()
        self.assertEqual(set(self.pushed), {PHONE_SIZE})

    async def test_select_calls_the_hook_with_the_tracker_id(self):
        selected: asyncio.Queue = asyncio.Queue()

        async def look(oid: int):
            selected.put_nowait(oid)
        webrtc.select_handler = look
        phone = await self.phone()
        msg = await phone.detections()
        for junk in ("not json", "[1]", '{"type": "hello"}', '{"type": "select"}',
                     '{"type": "select", "track_id": 3}', '{"type": "select", "track_id": "x3"}'):
            phone.channel.send(junk)
        track_id = msg["objects"][-1]["track_id"]
        phone.select(track_id)
        self.assertEqual(await asyncio.wait_for(selected.get(), 5), int(track_id[1:]))
        await asyncio.sleep(0.2)
        self.assertTrue(selected.empty())  # the junk before it called nothing

    async def test_select_without_a_hook_or_with_a_failing_one(self):
        phone = await self.phone()
        await phone.detections()
        with self.assertLogs("gtl.webrtc", logging.INFO) as logs:
            phone.select("t1")
            await until(lambda: any("no select handler" in line for line in logs.output))

        async def broken(oid):
            raise RuntimeError("look failed")
        webrtc.select_handler = broken
        with self.assertLogs("gtl.webrtc", logging.ERROR) as logs:
            phone.select("t2")
            await until(lambda: logs.records)
        self.assertIn("select t2 failed", logs.output[0])
        await phone.detections()  # the stream goes on

    async def test_a_second_offer_replaces_the_first(self):
        first = await self.phone()
        await first.detections()
        old = self.phones.current
        second = await self.phone()
        self.assertTrue(old.closed)
        self.assertEqual(old.pc.connectionState, "closed")
        self.assertIsNot(self.phones.current, old)
        await second.detections()
        self.assertEqual(self.stats()["open"], 1)
        self.assertEqual((self.stats()["created"], self.stats()["deleted"]), (2, 1))
        await until(lambda: first.pc.connectionState == "closed")  # the first phone hears about it
        self.assertEqual(first.channel.readyState, "closed")

    async def test_closing_the_phone_stops_tracking_and_deletes_the_session(self):
        phone = await self.phone()
        await phone.detections()
        self.assertTrue(self.live.running)
        await phone.close()
        await until(lambda: self.phones.current is None and not self.live.running)
        self.assertEqual(self.sam.state.sessions, {})
        self.assertEqual((self.stats()["created"], self.stats()["deleted"]), (1, 1))

    async def test_shutdown_closes_the_phone(self):
        phone = await self.phone()
        await phone.detections()
        await webrtc.close()
        self.assertIsNone(self.phones.current)
        self.assertFalse(self.live.running)
        self.assertEqual(self.sam.state.sessions, {})
        await until(lambda: phone.pc.connectionState == "closed")

    async def test_bad_offers(self):
        for body, status in (({"sdp": "v=0", "type": "answer"}, 400), ({"sdp": "garbage", "type": "offer"}, 400),
                             ({"type": "offer"}, 422)):
            r = await self.http.post("/offer", json=body)
            self.assertEqual(r.status_code, status, body)
        self.assertIsNone(self.phones.current)
        self.assertEqual(self.stats()["created"], 0)

    async def test_both_peers_use_the_same_ice_servers(self):
        servers = [{"urls": ["stun:stun.test:3478"]},
                   {"urls": ["turn:turn.test:3478?transport=udp"], "username": "u", "credential": "c"}]
        self.phones.ice = StaticIce(servers)
        configs = []

        def peer_connection(configuration):
            configs.append(configuration)
            return RTCPeerConnection(webrtc.rtc_configuration([]))  # don't really ask stun.test
        with mock.patch.object(webrtc, "RTCPeerConnection", peer_connection):
            phone = await self.phone(use_ice=False)
        r = await self.http.get("/ice-servers")
        self.assertEqual(r.json(), {"iceServers": servers})
        self.assertEqual(phone.ice_servers, servers)
        self.assertEqual([(s.urls, s.username, s.credential) for s in configs[0].iceServers],
                         [(["stun:stun.test:3478"], None, None), (["turn:turn.test:3478?transport=udp"], "u", "c")])
        await phone.detections()


# ---------- the stand-in phone, against a running server ----------

async def run_client(args):
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as http:
        phone = PhoneClient(http, Camera(args.video), codec=args.codec)
        t0 = time.perf_counter()
        print(f"phone: {Path(args.video).name} as {PHONE_SIZE[0]}x{PHONE_SIZE[1]} @ {PHONE_FPS} fps, "
              f"{args.codec} first, to {args.url}")
        try:
            await phone.connect()
            for s in phone.ice_servers:  # the URLs only: never print credentials
                print(f"ice: {', '.join(s['urls'])}{'  (with credentials)' if s.get('username') else ''}")
            print(f"answer: {webrtc.describe(phone.pc.remoteDescription.sdp)} ({time.perf_counter() - t0:.1f}s)")
            phone.pc.on("connectionstatechange", lambda: print(
                f"{time.perf_counter() - t0:5.1f}s connection {phone.pc.connectionState}"))
            labels: dict[str, set[str]] = {}
            first = selected = None
            count = 0
            deadline = time.monotonic() + args.seconds
            while (left := deadline - time.monotonic()) > 0:
                try:
                    msg = await asyncio.wait_for(phone.messages.get(), left)
                except TimeoutError:
                    break
                count += 1
                print(f"{time.perf_counter() - t0:5.1f}s {json.dumps(msg)}")
                for o in msg.get("objects", []):
                    labels.setdefault(o["track_id"], set()).add(o["label"])
                    first = first or time.perf_counter() - t0
                if selected is None and msg.get("objects"):
                    selected = msg["objects"][0]["track_id"]
                    phone.select(selected)
                    print(f"{time.perf_counter() - t0:5.1f}s sent select {selected}")
        finally:
            await phone.close()
        print(f"\n{count} messages; first objects after {first:.1f}s" if first else f"\n{count} messages; no objects")
        for track_id, names in sorted(labels.items(), key=lambda kv: int(kv[0][1:])):
            print(f"  {track_id}: {', '.join(sorted(names))}{'  <- label changed' if len(names) > 1 else ''}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client", action="store_true", help="play the phone against a running server")
    p.add_argument("--url", default="http://127.0.0.1:8004")
    p.add_argument("--video", default="test_media/synthetic_room.mp4")
    p.add_argument("--seconds", type=float, default=30)
    p.add_argument("--codec", default="H264", choices=["H264", "VP8"])
    args, rest = p.parse_known_args()
    if args.client:
        if not Path(args.video).is_file():
            p.error(f"no such file: {args.video}")
        try:
            asyncio.run(run_client(args))
        except KeyboardInterrupt:
            pass
    else:
        unittest.main(argv=[sys.argv[0], *rest])
