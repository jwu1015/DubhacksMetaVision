"""Tests for /detect and /identify inputs: track_id (no photo), hint_point, hint_text and their
precedence. SAM (segment.sam_objects) and Claude (vision's HTTP transport) are mocked and the store
search is stubbed: no network, no GPU.

    python -m unittest test_detect -v                                  (from backend/)
"""
import asyncio
import base64
import io
import json
import logging
import time
import unittest
from contextlib import asynccontextmanager
from unittest import mock

import anthropic
import httpx2
from fastapi.testclient import TestClient
from PIL import Image

import config
import main
import segment
import stream
import vision
from models import BBox
from segment import Found
from tracker import TrackedObject, TrackResult

logging.getLogger("gtl").setLevel(logging.CRITICAL)  # the fallback tests log expected warnings
for name in ("httpx", "httpx2"):  # one line per mocked request
    logging.getLogger(name).setLevel(logging.WARNING)

PHOTO = (config.FIXTURES / "lamp_room.jpg").read_bytes()
W, H = Image.open(io.BytesIO(PHOTO)).size

LAMP = Found(BBox(x=0.33, y=0.14, w=0.22, h=0.71), 0.95, "lamp")  # covers the center, in front of the sofa
SOFA = Found(BBox(x=0.05, y=0.4, w=0.9, h=0.5), 0.9, "sofa")
LEFT = Found(BBox(x=0.1, y=0.2, w=0.2, h=0.4), 0.8, "chair")    # x 0.1-0.3
RIGHT = Found(BBox(x=0.6, y=0.2, w=0.3, h=0.4), 0.85, "shelf")  # x 0.6-0.9

GOOD = {"category": "green floor lamp", "description": "green metal floor lamp with a dome shade",
        "style": "mid-century modern", "colors": ["green"], "materials": ["metal"], "brand_guess": None,
        "confidence": 0.9, "search_queries": ["green dome floor lamp", "floor lamp"],
        "bbox": {"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1}, "quick_phrase": "Green floor lamp. Finding prices."}


def close(a: dict, b: BBox, tol: float = 0.002) -> bool:
    return all(abs(a[k] - getattr(b, k)) < tol for k in "xywh")


@asynccontextmanager
async def no_lifespan(app):
    yield  # no warm-up, SAM health check or TURN credentials: those reach the network


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.sam_reply: list[Found] | None = [LAMP, SOFA]
        self.sam_calls: list[str | None] = []  # the prompt of each SAM call
        self.claude_calls: list[dict] = []     # {"text", "size"} of each Claude call

        async def sam_objects(image, prompt, timeout=segment.SAM_TIMEOUT):
            self.sam_calls.append(prompt)
            return self.sam_reply

        async def claude(request):
            content = json.loads(request.content)["messages"][0]["content"]
            image = next(b for b in content if b["type"] == "image")
            self.claude_calls.append({
                "text": next(b["text"] for b in content if b["type"] == "text"),
                "size": Image.open(io.BytesIO(base64.b64decode(image["source"]["data"]))).size})
            return httpx2.Response(200, json={
                "id": "m", "type": "message", "role": "assistant", "model": vision.MODEL,
                "content": [{"type": "text", "text": json.dumps(GOOD)}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}})

        async def no_search(cap):  # the store search isn't under test: an empty answer, at once
            return main.empty_search(cap.capture_id)

        claude_client = anthropic.AsyncAnthropic(api_key="test", max_retries=0, http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(claude)))
        self.live = stream.Stream()
        for target, name, value in ((config, "USE_FIXTURES", False), (config, "MODEL_SERVICE_URL", "https://sam.test"),
                                    (config, "PUBLIC_BASE_URL", "http://testserver"),
                                    (segment, "sam_objects", sam_objects), (vision, "_client", claude_client),
                                    (main, "search_pipeline", no_search), (stream, "live", self.live),
                                    (main.app.router, "lifespan_context", no_lifespan)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.remove_uploads)
        self.cids: list[str] = []

    def remove_uploads(self):
        for cid in self.cids:
            for path in config.UPLOADS.glob(f"{cid}*"):
                path.unlink()

    def client(self, host: str = "127.0.0.1") -> TestClient:
        return TestClient(main.app, client=(host, 50000))

    def post(self, c: TestClient, route: str = "/detect", photo: bool = True, headers=None, **form):
        files = {"image": ("photo.jpg", PHOTO, "image/jpeg")} if photo else None
        r = c.post(route, files=files, data=form, headers=headers)
        if r.status_code == 200:
            self.cids.append((r.json()["detect"] if route == "/identify" else r.json())["capture_id"])
        return r

    def described(self, c: TestClient, cid: str) -> dict:
        """The item once Claude has described it (/search waits for that)."""
        return c.post(f"/search/{cid}").json()["item"]

    def track(self, *objects: TrackedObject, frame: Image.Image | None = None):
        """The live stream is tracking these objects in frame (lamp_room.jpg by default)."""
        frame = frame or Image.open(io.BytesIO(PHOTO)).convert("RGB")
        self.live._tracked = stream.Frame(frame, time.time(), 7)
        self.live._result = TrackResult(frame_idx=12, width=640, height=480, latency_ms=900,
                                        captured_at=time.time(), objects=list(objects))

    # ---------- track_id ----------

    def test_track_id(self):
        frame = Image.open(io.BytesIO(PHOTO)).convert("RGB").resize((640, 480))
        self.track(TrackedObject(id=3, label="floor lamp", score=0.91, box=LAMP.box),
                   TrackedObject(id=4, label="rug", score=0.8, box=BBox(x=0.1, y=0.8, w=0.8, h=0.2)), frame=frame)
        with self.client() as c:
            r = self.post(c, photo=False, track_id="t3")
            self.assertEqual(r.status_code, 200, r.text)
            d = r.json()
            self.assertEqual(d["item"]["category"], "floor lamp")
            self.assertEqual(d["quick_phrase"], "Floor lamp. Finding prices.")
            self.assertTrue(close(d["segmentation"]["bbox"], LAMP.box))
            self.assertEqual(d["segmentation"]["mask_score"], 0.91)
            # image_url is the frame that was used: the box above is relative to it
            self.assertEqual(d["image_url"], f"http://testserver/uploads/{d['capture_id']}.jpg")
            with Image.open(config.UPLOADS / f"{d['capture_id']}.jpg") as saved:
                self.assertEqual(saved.size, (640, 480))
            self.assertEqual(main.capture_tracks[d["capture_id"]], 3)
            self.assertEqual(self.described(c, d["capture_id"])["category"], "green floor lamp")
        self.assertEqual(self.sam_calls, [])  # the track's box is the hint
        self.assertEqual(len(self.claude_calls), 1)
        self.assertLess(self.claude_calls[0]["size"][0], 640)  # Claude saw just the crop

    def test_track_id_on_identify(self):
        self.track(TrackedObject(id=3, label="floor lamp", score=0.91, box=LAMP.box))
        with self.client() as c:
            r = self.post(c, "/identify", photo=False, track_id="t3")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["detect"]["item"]["category"], "floor lamp")
        self.assertEqual(r.json()["search"]["item"]["category"], "green floor lamp")

    def test_track_id_through_the_tunnel(self):
        """Unlike POST /look/{id}, the phone reaches this through the tunnel."""
        self.track(TrackedObject(id=3, label="floor lamp", score=0.91, box=LAMP.box))
        with self.client("203.0.113.9") as c:
            r = self.post(c, photo=False, track_id="t3", headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_track_expired(self):
        with self.client() as c:
            r = self.post(c, photo=False, track_id="t3")  # not tracking anything
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "track expired"}))
            self.track(TrackedObject(id=3, label="floor lamp", score=0.91, box=LAMP.box))
            r = self.post(c, photo=False, track_id="t9")  # left the window
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "track expired"}))
            r = self.post(c, "/identify", photo=False, track_id="t9")
            self.assertEqual((r.status_code, r.json()), (404, {"detail": "track expired"}))

    def test_image_or_track_id(self):
        self.track(TrackedObject(id=3, label="floor lamp", score=0.91, box=LAMP.box))
        with self.client() as c:
            for route in ("/detect", "/identify"):
                for photo, form in ((False, {}), (True, {"track_id": "t3"}), (False, {"hint_text": "lamp"})):
                    r = self.post(c, route, photo=photo, **form)
                    self.assertEqual(r.status_code, 400, (route, photo, form))
                    self.assertIn("exactly one of image", r.json()["detail"])
            for bad in ("x3", "t", "three", "t3.5"):
                r = self.post(c, photo=False, track_id=bad)
                self.assertEqual(r.status_code, 400, bad)
                self.assertIn('"t4"', r.json()["detail"])
        self.assertEqual((self.sam_calls, self.claude_calls), ([], []))

    # ---------- hint_point ----------

    def test_point_inside_boxes(self):
        """The smallest box containing the point: the lamp in front of the sofa, not the sofa."""
        with self.client() as c:
            d = self.post(c, hint_point='{"x": 0.45, "y": 0.6}').json()
            self.assertEqual(d["item"]["category"], "lamp")
            self.assertEqual(d["quick_phrase"], "Lamp. Finding prices.")
            self.assertEqual(d["segmentation"]["method"], "sam3")
            self.assertTrue(close(d["segmentation"]["bbox"], LAMP.box))
            self.assertEqual(self.described(c, d["capture_id"])["category"], "green floor lamp")
            d = self.post(c, hint_point="[0.2, 0.6]").json()  # the sofa only
            self.assertEqual(d["item"]["category"], "sofa")
        self.assertEqual(self.sam_calls, [segment.SAM_CATEGORIES or None] * 2)
        self.assertLess(self.claude_calls[0]["size"][0], W // 2)  # Claude described just the crop
        self.assertEqual(self.claude_calls[0]["text"], "What is the main item in this photo?")

    def test_point_between_boxes(self):
        self.sam_reply = [RIGHT, LEFT]
        with self.client() as c:
            d = self.post(c, hint_point='{"x": 0.36, "y": 0.4}').json()  # 0.06 from LEFT, 0.24 from RIGHT
            self.assertEqual(d["item"]["category"], "chair")
            d = self.post(c, hint_point='{"x": 0.45, "y": 0.9}').json()  # nothing near: a crop around the point
            self.assertEqual(d["segmentation"]["method"], "bbox_fallback")
            self.assertTrue(close(d["segmentation"]["bbox"], segment.point_box(0.45, 0.9)))
            self.assertEqual(d["quick_phrase"], "Got it. Finding prices.")
            self.assertEqual(self.described(c, d["capture_id"])["category"], "green floor lamp")

    def test_point_sam_down(self):
        self.sam_reply = None
        with self.client() as c:
            r = self.post(c, hint_point='{"x": 0.5, "y": 0.5}', hint_text="green floor lamp")
            self.assertEqual(r.status_code, 200, r.text)
            d = r.json()
            box = BBox(x=0.3, y=0.3, w=segment.POINT_CROP, h=segment.POINT_CROP)
            self.assertEqual(d["segmentation"]["method"], "bbox_fallback")
            self.assertTrue(close(d["segmentation"]["bbox"], box))
            self.assertTrue(close(d["item"]["bbox"], box))
            self.assertEqual(d["item"]["category"], "green floor lamp")  # what the user said, until Claude answers
            item = self.described(c, d["capture_id"])
            self.assertEqual(item["description"], GOOD["description"])
            # Near the edge, the crop keeps its size and moves inside the photo.
            d = self.post(c, hint_point='{"x": 0.98, "y": 0.02}').json()
            self.assertTrue(close(d["segmentation"]["bbox"], BBox(x=0.6, y=0.0, w=0.4, h=0.4)))
        self.assertEqual(self.claude_calls[0]["text"],
                         "The user asked about: green floor lamp\nWhat is the main item in this photo?")

    # ---------- hint_text ----------

    def test_text(self):
        with self.client() as c:
            d = self.post(c, hint_text="  green floor\nlamp ").json()
            self.assertEqual(d["item"]["category"], "lamp")  # SAM's object at the center of the view
            self.assertEqual(d["segmentation"]["method"], "sam3")
            self.described(c, d["capture_id"])
        self.assertEqual(self.sam_calls, ["green floor lamp"])
        # Claude saw the whole photo, then (looking elsewhere) the crop, with the hint both times
        self.assertEqual([call["size"] for call in self.claude_calls][0], (W, H))
        self.assertEqual(len(self.claude_calls), 2)
        for call in self.claude_calls:
            self.assertEqual(call["text"], "The user asked about: green floor lamp\nWhat is the main item in this photo?")

    def test_text_best_score_off_center(self):
        corner_a = Found(BBox(x=0.0, y=0.0, w=0.15, h=0.15), 0.7, "lamp")
        corner_b = Found(BBox(x=0.85, y=0.85, w=0.15, h=0.15), 0.9, "lamp")
        tiny = Found(BBox(x=0.45, y=0.45, w=0.1, h=0.1), 0.99, "lamp")  # 1% of the photo: ignored
        self.sam_reply = [tiny, corner_a, corner_b]
        with self.client() as c:
            d = self.post(c, hint_text="green floor lamp").json()
        self.assertTrue(close(d["segmentation"]["bbox"], corner_b.box))

    def test_text_nothing_found(self):
        """SAM finds nothing for the words: Claude first, with the hint."""
        self.sam_reply = []
        with self.client() as c:
            d = self.post(c, hint_text="green floor lamp").json()
        self.assertEqual(d["item"]["category"], "green floor lamp")
        self.assertEqual(d["item"]["description"], GOOD["description"])
        self.assertEqual(d["segmentation"]["method"], "bbox_fallback")
        self.assertEqual(self.sam_calls, ["green floor lamp", "green floor lamp"])  # then Claude's category
        self.assertEqual([call["size"] for call in self.claude_calls], [(W, H)])
        self.assertTrue(self.claude_calls[0]["text"].startswith("The user asked about: green floor lamp\n"))

    def test_text_is_cut_to_one_short_line(self):
        with self.client() as c:
            self.post(c, hint_text="lamp " * 50)
        self.assertEqual(len(self.sam_calls[0]), main.MAX_HINT_TEXT)

    def test_no_hints_is_unchanged(self):
        with self.client() as c:
            d = self.post(c).json()
        self.assertEqual(d["item"]["category"], "lamp")
        self.assertEqual(self.sam_calls, [segment.SAM_CATEGORIES or None])
        self.assertEqual(self.claude_calls[0]["text"], "What is the main item in this photo?")

    # ---------- precedence and bad input ----------

    def test_box_beats_point_and_text(self):
        box = {"x": 0.6, "y": 0.2, "w": 0.3, "h": 0.4}
        with self.client() as c:
            d = self.post(c, hint_box=json.dumps(box), hint_label="shelf", hint_point='{"x": 0.45, "y": 0.6}',
                          hint_text="green floor lamp").json()
            self.assertEqual(d["item"]["category"], "shelf")
            self.assertTrue(close(d["segmentation"]["bbox"], BBox(**box)))
            self.described(c, d["capture_id"])
            d = self.post(c, hint_box=json.dumps(box), hint_text="green floor lamp").json()
            self.assertEqual(d["item"]["category"], "green floor lamp")  # no hint_label: the user's words
        self.assertEqual(self.sam_calls, [])
        # Claude still hears what the user asked about
        self.assertTrue(self.claude_calls[0]["text"].startswith("The user asked about: green floor lamp\n"))

    def test_point_beats_text(self):
        self.sam_reply = [RIGHT, LEFT]
        with self.client() as c:
            d = self.post(c, hint_point="[0.7, 0.3]", hint_text="green floor lamp").json()
        self.assertEqual(d["item"]["category"], "shelf")  # at the point, not the best score
        self.assertEqual(self.sam_calls, ["green floor lamp"])  # the words are still SAM's prompt

    def test_bad_hints(self):
        with self.client() as c:
            for bad in ("nope", "[0.5]", "[0.1, 0.2, 0.3]", '{"x": 0.5}', '{"x": "a", "y": 0.5}', "[null, 0.5]",
                        "5", '"0.5, 0.5"', '{"x": 1.5, "y": 0.5}', "[-0.1, 0.5]", "[NaN, 0.5]"):
                for route in ("/detect", "/identify"):
                    r = self.post(c, route, hint_point=bad)
                    self.assertEqual(r.status_code, 400, (route, bad, r.text))
                    self.assertIn("hint_point must be", r.json()["detail"])
            r = self.post(c, hint_point="nope")
            self.assertIn('{"x": 0.45, "y": 0.6} or [x, y]', r.json()["detail"])
            r = self.post(c, hint_box='{"x": 0.3}', hint_point='{"x": 0.5, "y": 0.5}')
            self.assertEqual(r.status_code, 400)
            self.assertIn('{"x": 0.3, "y": 0.1, "w": 0.2, "h": 0.7}', r.json()["detail"])
            r = self.post(c, hint_box='{"x": 0.3, "y": 0.1, "w": 0.2, "h": 0.7}', hint_point="nope")
            self.assertEqual(r.status_code, 400)  # a bad hint is reported even when another one wins
        self.assertEqual((self.sam_calls, self.claude_calls), ([], []))


class PickTest(unittest.TestCase):
    """segment.py's helpers on their own."""

    def test_point_object(self):
        self.assertEqual(segment.point_object([SOFA, LAMP], 0.45, 0.6), LAMP)
        self.assertEqual(segment.point_object([SOFA, LAMP], 0.2, 0.6), SOFA)
        self.assertEqual(segment.point_object([RIGHT, LEFT], 0.36, 0.4), LEFT)
        self.assertEqual(segment.point_object([RIGHT, LEFT], 0.55, 0.4), RIGHT)  # 0.05 from RIGHT, 0.25 from LEFT
        self.assertIsNone(segment.point_object([RIGHT, LEFT], 0.45, 0.9))
        self.assertIsNone(segment.point_object([], 0.5, 0.5))
        tiny = Found(BBox(x=0.4, y=0.5, w=0.1, h=0.1), 0.99, "vase")
        self.assertEqual(segment.point_object([tiny, SOFA], 0.45, 0.55), SOFA)

    def test_text_object(self):
        self.assertEqual(segment.text_object([SOFA, LAMP]), LAMP)  # at the center
        self.assertEqual(segment.text_object([LEFT, RIGHT]), RIGHT)  # LEFT is too far from the center
        self.assertIsNone(segment.text_object([]))

    def test_point_box(self):
        self.assertEqual(segment.point_box(0.5, 0.5), BBox(x=0.3, y=0.3, w=0.4, h=0.4))
        self.assertEqual(segment.point_box(0.0, 1.0), BBox(x=0.0, y=0.6, w=0.4, h=0.4))


if __name__ == "__main__":
    unittest.main()
