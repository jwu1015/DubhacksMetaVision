"""Tests for the voice assistant's tool (voice/assistant.py) and the object it says the camera is on
(voice/seen.py). The backend's messages go to Seen as they'd arrive in the room: no LiveKit, no Gemini."""
import asyncio
import json
import unittest
from unittest import mock

from storefront.voice import assistant
from storefront.voice.assistant import Assistant
from storefront.voice.seen import Seen


def thing(uid, prompt, box, in_view=True):
    return {"uid": uid, "prompt": prompt, "score": 0.9, "box": box, "in_view": in_view, "last_seen_ts": 0}


SOFA = thing("1.0", "sofa", [0.10, 0.30, 0.90, 0.90])
MUG = thing("1.1", "mug", [0.45, 0.40, 0.60, 0.60])      # on the sofa, at the center of the view
LAMP = thing("1.2", "lamp", [0.70, 0.05, 0.85, 0.25])
VASE = thing("1.3", "vase", [0.05, 0.40, 0.20, 0.55])    # nearer the center than the lamp
CHAIR = thing("1.4", "chair", [0.40, 0.40, 0.60, 0.60], in_view=False)  # passed a moment ago


def objects(*things):
    return {"type": "objects", "session": 1, "age_ms": 300, "objects": list(things)}


def product(title, store, price, tier="new", score=None):
    return {"title": title, "store": store, "price": price, "currency": "USD", "url": "https://example.com",
            "image_url": None, "source": "shopping", "tier": tier, "source_rank": 0, "match_score": score,
            "savings_pct": None, "match_reason": None}


SEARCH = {"capture_id": "l_1", "spoken_summary": "Target has it for about 12 dollars.",
          "best_match": product("Stoneware Mug 16oz", "Target", 11.99, score=88),
          "dupes": [product("White Ceramic Mug", "Amazon", 7.49, "dupe")], "used": [],
          "price_insight": {"typical_low": 6.0, "typical_high": 18.0, "median": 11.0, "sample_size": 9,
                            "best_match_verdict": "fair", "verdict_text": "That's a fair price."},
          "timing_ms": {"total": 2400}, "item": None}
LABEL = {"type": "label", "uid": "1.1", "box": MUG["box"], "name": "white ceramic mug", "price_low": 8,
         "price_high": 15}


def look(uid, stage):
    return {"type": "look", "uid": uid, "look_id": f"l_{uid}", "stage": stage, "label": "mug", "box": [0, 0, 1, 1],
            "selected": False, "crop_url": None,
            "item": {"category": "coffee mug", "description": "Glossy white stoneware."} if stage != "started" else None,
            "search": SEARCH if stage == "results" else None, "message": "Couldn't get prices." if stage == "failed" else None}


def app_data(said: str) -> dict:
    return json.loads(said.split("<app_data>")[1].split("</app_data>")[0])


class CurrentTest(unittest.TestCase):
    def setUp(self):
        self.seen = Seen()

    def test_the_smallest_object_at_the_center(self):
        self.seen.update(objects(SOFA, MUG, LAMP))
        self.assertIs(self.seen.current(), MUG)

    def test_else_the_nearest_to_the_center(self):
        self.seen.update(objects(LAMP, VASE))
        self.assertIs(self.seen.current(), VASE)

    def test_only_objects_in_view(self):
        self.seen.update(objects(LAMP, CHAIR))
        self.assertIs(self.seen.current(), LAMP)
        self.seen.update(objects(CHAIR))
        self.assertIsNone(self.seen.current())

    def test_a_restart_forgets_the_labels_and_looks(self):
        for m in (objects(MUG), LABEL, look("1.1", "results")):
            self.seen.update(m)
        self.seen.update({"type": "status", "tracking": "stopped", "category": "small things to buy", "prompts": []})
        self.assertEqual((self.seen.labels, self.seen.looks), ({}, {}))


class GetCurrentObjectTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.seen = Seen()
        self.seen.update(objects(SOFA, MUG, LAMP))
        self.seen.update(LABEL)
        self.agent = Assistant(self.seen)

    async def test_what_the_camera_is_on_and_its_prices(self):
        self.seen.update(look("1.1", "results"))
        data = app_data(await self.agent.get_current_object(None))
        self.assertEqual((data["tracked_as"], data["box"], data["looks_like"], data["price_guess"], data["item"]),
                         ("mug", MUG["box"], "white ceramic mug", "$8-15", "coffee mug"))
        self.assertEqual((data["best_match"]["store"], data["best_match"]["same_product"]), ("Target", True))
        self.assertEqual((data["dupes"][0]["store"], data["dupes"][0]["same_product"]), ("Amazon", False))
        self.assertEqual(data["price_check"], "That's a fair price.")

    async def test_it_waits_for_the_prices(self):
        self.seen.update(look("1.1", "started"))

        async def prices_come_in():
            await asyncio.sleep(0.3)
            self.seen.update(look("1.1", "described"))
            await asyncio.sleep(0.3)
            self.seen.update(look("1.1", "results"))
        asyncio.create_task(prices_come_in())
        data = app_data(await self.agent.get_current_object(None))
        self.assertEqual(data["best_match"]["title"], "Stoneware Mug 16oz")

    async def test_prices_still_loading(self):
        self.seen.update(look("1.1", "described"))
        with mock.patch.object(assistant, "RESULTS_WAIT_S", 0.3):
            data = app_data(await self.agent.get_current_object(None))
        self.assertEqual((data["item"], data["price_guess"], data["store_prices"]), ("coffee mug", "$8-15", "still loading"))
        self.assertNotIn("best_match", data)

    async def test_prices_failed(self):
        self.seen.update(look("1.1", "failed"))
        data = app_data(await self.agent.get_current_object(None))
        self.assertEqual(data["store_prices"], "not found")

    async def test_nothing_in_view(self):
        self.seen.update(objects(CHAIR))
        self.assertEqual(await self.agent.get_current_object(None), "Nothing is in view right now.")
