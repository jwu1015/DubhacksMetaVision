"""Tests for segment.py against a fake segmentation server (httpx.MockTransport): no GPU, no network.

The server only takes tracking sessions with labels (18 Oct), so a photo goes through a one-frame session:
POST /session, POST /session/{id}/frame, DELETE. Without labels, Claude picks them first (labels.py).
"""
import asyncio
import io
import json
import unittest
from unittest import mock

import httpx
from PIL import Image

import config
import net
import segment
from models import BBox, Item

BASE = "https://seg.test/inference"
W, H = 1024, 768  # the photo; the server answers in pixels of the 640px copy it gets
LAMP = [210.6, 66.9, 353.1, 409.4]  # x1, y1, x2, y2 at 640x480: x 0.329-0.552, y 0.139-0.853
SOFA = [64.0, 240.0, 600.0, 450.0]
PHOTO = Image.open(config.FIXTURES / "lamp_room.jpg").convert("RGB")
ITEM = Item(category="floor lamp", bbox=BBox(x=0.3, y=0.1, w=0.3, h=0.78))


def reply(*objects):
    return {"frame_idx": 0, "width": 640, "height": 480, "labels": [],
            "objects": [{"id": i, "score": s, "box": b, "prompt": p} for i, (b, s, p) in enumerate(objects)]}


class FakeServer:
    """Baptiste's API: sessions take our labels."""

    def __init__(self, frame=None, create=200, delay=0.0):
        self.frame, self.create, self.delay = frame or reply(), create, delay
        self.requests: list[httpx.Request] = []
        self.sessions: dict[str, str] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/inference")
        if request.method == "POST" and path == "/session":
            if self.create != 200:
                return httpx.Response(self.create, json={"detail": "nope"})
            sid = f"s{len(self.sessions) + 1}"
            self.sessions[sid] = json.loads(request.content)["prompt"]
            return httpx.Response(200, json={"session_id": sid, "labels": self.sessions[sid].split(",")})
        if request.method == "POST" and path.endswith("/frame"):
            if self.delay:
                await asyncio.sleep(self.delay)
            frame = self.frame
            return httpx.Response(frame, json={"detail": "boom"}) if isinstance(frame, int) else httpx.Response(200, json=frame)
        if request.method == "DELETE":
            self.sessions.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(404, json={"detail": "no route"})

    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.path.removeprefix('/inference')}" for r in self.requests]


class SegmentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, name, value in ((config, "MODEL_SERVICE_URL", BASE), (config, "CF_ACCESS_CLIENT_ID", ""),
                                    (config, "CF_ACCESS_CLIENT_SECRET", "")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def serve(self, server: FakeServer):
        # follow_redirects=True like net.client(), so a request that forgets to turn it off would show.
        client = httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)
        self.addAsyncCleanup(client.aclose)
        patcher = mock.patch.object(net, "_client", client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return server

    async def settle(self):
        """Let the background session deletes finish."""
        await asyncio.gather(*list(segment._deleting))

    def tearDown(self):
        for p in (config.ROOT / "uploads").glob("test_seg*"):
            p.unlink()

    async def test_our_labels_go_through_a_one_frame_session(self):
        server = self.serve(FakeServer(reply((SOFA, 0.81, "sofa"), (LAMP, 0.93, "lamp"))))
        found = await segment.sam_objects(PHOTO, "sofa,lamp")
        await self.settle()
        self.assertEqual(server.calls(), ["POST /session", "POST /session/s1/frame", "DELETE /session/s1"])
        self.assertEqual(json.loads(server.requests[0].content), {"prompt": "sofa,lamp"})
        sent = Image.open(io.BytesIO(server.requests[1].content))
        self.assertEqual((sent.format, max(sent.size)), ("JPEG", 640))
        self.assertEqual(server.requests[1].headers["content-type"], "image/jpeg")
        self.assertEqual([(f.label, f.score) for f in found], [("lamp", 0.93), ("sofa", 0.81)])  # best first
        lamp = found[0].box
        self.assertAlmostEqual(lamp.x, 210.6 / 640, places=3)
        self.assertAlmostEqual(lamp.h, (409.4 - 66.9) / 480, places=3)
        self.assertEqual(server.sessions, {})  # nothing left on the server

    async def test_no_labels_has_claude_pick_them_before_the_session(self):
        server = self.serve(FakeServer(reply((LAMP, 0.92, "lamp"))))
        picks = []

        async def fake_labels(image, category):
            picks.append((max(image.size), category, len(server.requests)))
            return ["lamp", "rug"]
        with mock.patch("labels.labels_or_fallback", fake_labels):
            found = await segment.sam_objects(PHOTO, None)
        await self.settle()
        self.assertEqual(picks, [(640, config.SAM_AUTO_CATEGORY, 0)])  # the photo sent, before any request
        self.assertEqual(server.calls(), ["POST /session", "POST /session/s1/frame", "DELETE /session/s1"])
        self.assertEqual(json.loads(server.requests[0].content), {"prompt": "lamp,rug"})
        self.assertEqual([f.label for f in found], ["lamp"])

    async def test_failures_give_none_and_never_leave_a_session(self):
        for server, why in ((FakeServer(create=503), "too many sessions"), (FakeServer(500), "frame failed"),
                            (FakeServer({"nope": 1}), "unexpected JSON")):
            with self.subTest(why):
                self.serve(server)
                self.assertIsNone(await segment.sam_objects(PHOTO, "lamp"))
                await self.settle()
                self.assertEqual(server.sessions, {})

    async def test_a_slow_server_is_cut_off_and_its_session_still_deleted(self):
        server = self.serve(FakeServer(reply((LAMP, 0.9, "lamp")), delay=5.0))
        t0 = asyncio.get_running_loop().time()
        self.assertIsNone(await segment.sam_objects(PHOTO, "lamp", timeout=0.3))
        self.assertLess(asyncio.get_running_loop().time() - t0, 1.0)
        await self.settle()
        self.assertEqual(server.calls()[-1], "DELETE /session/s1")

    async def test_access_headers_and_no_redirects(self):
        server = self.serve(FakeServer(reply((LAMP, 0.9, "lamp"))))
        with mock.patch.multiple(config, CF_ACCESS_CLIENT_ID="id.access", CF_ACCESS_CLIENT_SECRET="s3cret"):
            await segment.sam_objects(PHOTO, "lamp")
            await self.settle()
        for r in server.requests:
            self.assertEqual((r.headers.get("cf-access-client-id"), r.headers.get("cf-access-client-secret")),
                             ("id.access", "s3cret"), r.url.path)
        for status in (302, 401, 403):
            with self.subTest(status):
                server = self.serve(FakeServer(create=status))
                self.assertIsNone(await segment.sam_objects(PHOTO, "lamp"))
                self.assertEqual(server.calls(), ["POST /session"])  # a login redirect isn't followed

    async def test_no_server_configured_sends_nothing(self):
        server = self.serve(FakeServer())
        with mock.patch.object(config, "MODEL_SERVICE_URL", ""):
            self.assertIsNone(await segment.sam_objects(PHOTO, "lamp"))
        self.assertEqual(server.requests, [])

    async def test_claude_first_crop(self):
        """segment(): SAM's box for Claude's category when it overlaps Claude's, else Claude's padded box."""
        cases = (
            ("overlap preferred", reply((SOFA, 0.95, "floor lamp"), (LAMP, 0.70, "floor lamp")), "sam3", 0.70),
            ("a lone box is used", reply(([500.0, 300.0, 630.0, 470.0], 0.99, "floor lamp")), "sam3", 0.99),
            ("several, none overlapping", reply(([0.0, 0.0, 60.0, 60.0], 0.9, "floor lamp"),
                                                ([580.0, 400.0, 640.0, 480.0], 0.8, "floor lamp")), "bbox_fallback", None),
            ("SAM down", 500, "bbox_fallback", None),
        )
        for why, frame, method, score in cases:
            with self.subTest(why):
                server = self.serve(FakeServer(frame))
                seg = await segment.segment(PHOTO, ITEM, "test_seg")
                await self.settle()
                self.assertEqual((seg.method, seg.mask_score), (method, score))
                self.assertEqual(json.loads(server.requests[0].content), {"prompt": "floor lamp"})
                if method == "bbox_fallback":
                    self.assertEqual(seg.bbox, segment.padded_box(ITEM.bbox))
                self.assertTrue((config.ROOT / seg.cutout_path).exists())


if __name__ == "__main__":
    unittest.main()
