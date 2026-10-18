"""Tests for the voice assistant (voice/assistant.py) and what it takes "this" to mean (voice/seen.py). The
backend's messages go to Seen as they'd arrive in the room, and the RPCs to a stand-in room: no LiveKit, no Gemini."""
import asyncio
import json
import unittest
from unittest import mock

from livekit.agents import ToolError

from fakes.livekit import FakeLocalParticipant, FakeParticipant, FakeRoom
from storefront.voice import actions, assistant, seen as seen_module
from storefront.voice.assistant import Assistant
from storefront.voice.seen import Seen


def thing(uid, prompt, box, in_view=True):
    return {"uid": uid, "prompt": prompt, "score": 0.9, "box": box, "in_view": in_view, "last_seen_ts": 0}


SOFA = thing("1.0", "sofa", [0.10, 0.30, 0.90, 0.90])
MUG = thing("1.1", "mug", [0.45, 0.40, 0.60, 0.60])      # on the sofa, at the center of the view
LAMP = thing("1.2", "lamp", [0.70, 0.05, 0.85, 0.25])    # top right, far from the center
VASE = thing("1.3", "vase", [0.05, 0.40, 0.20, 0.55])    # left, far from the center
CHAIR = thing("1.4", "chair", [0.40, 0.40, 0.60, 0.60], in_view=False)  # passed a moment ago


def objects(*things, session=1):
    return {"type": "objects", "session": session, "age_ms": 300, "objects": list(things)}


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


def look(uid, stage, selected=False):
    return {"type": "look", "uid": uid, "look_id": f"l_{uid}", "stage": stage, "label": "mug", "box": [0, 0, 1, 1],
            "selected": selected, "crop_url": None,
            "item": {"category": "coffee mug", "description": "Glossy white stoneware."} if stage != "started" else None,
            "search": SEARCH if stage == "results" else None, "message": "Couldn't get prices." if stage == "failed" else None}


def app_data(said: str) -> dict:
    return json.loads(said.split("<app_data>")[1].split("</app_data>")[0])


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CurrentTest(unittest.TestCase):
    def setUp(self):
        self.seen = Seen()

    def test_the_mug_in_front_of_the_sofa(self):
        self.seen.update(objects(SOFA, MUG, LAMP))
        self.assertIs(self.seen.current(), MUG)

    def test_nothing_when_nothing_is_near_the_center(self):  # not the lamp at the edge, however near it's nearest
        self.seen.update(objects(LAMP, VASE))
        self.assertIsNone(self.seen.current())

    def test_only_objects_in_view(self):
        self.seen.update(objects(MUG, CHAIR))
        self.assertIs(self.seen.current(), MUG)
        self.seen.update(objects(CHAIR))
        self.assertIsNone(self.seen.current())

    def test_a_restart_forgets_the_labels_and_looks(self):
        for m in (objects(MUG), LABEL, look("1.1", "results")):
            self.seen.update(m)
        self.seen.update({"type": "status", "tracking": "stopped", "category": "small things to buy", "prompts": []})
        self.assertEqual((self.seen.labels, self.seen.looks), ({}, {}))

    def test_an_empty_view_isnt_a_restart_but_a_new_session_is(self):
        for m in (objects(MUG), LABEL, look("1.1", "results")):
            self.seen.update(m)
        self.seen.update(objects())  # nothing tracked for a while
        self.assertEqual((list(self.seen.labels), list(self.seen.looks)), (["1.1"], ["1.1"]))
        self.seen.update(objects(session=2))  # tracking restarted: uids start over
        self.assertEqual((self.seen.labels, self.seen.looks), ({}, {}))


class AimTest(unittest.TestCase):
    """What's in front when objects crowd the center: what the camera points at squarely, and keeps pointing at,
    not the smallest box."""

    def setUp(self):
        self.clock = Clock()
        self.seen = Seen(clock=self.clock)

    def view(self, *things, seconds=0.1, steps=1):
        """The tracker's objects messages for one view, `steps` of them over `seconds`."""
        for _ in range(steps):
            self.seen.update(objects(*things))
            self.clock.now += seconds / steps

    def test_the_speaker_in_front_not_the_charger_behind_it(self):
        speaker = thing("2.0", "speaker", [0.30, 0.30, 0.70, 0.75])  # aimed at
        charger = thing("2.1", "charger", [0.45, 0.15, 0.62, 0.55])  # smaller, covers the center, sticks out above
        self.view(speaker, charger)
        self.assertIs(self.seen.current(), speaker)  # the smallest box at the center was the charger
        self.assertIsNone(self.seen.rival(speaker))

    def test_a_tiny_box_isnt_what_they_point_at(self):
        book = thing("2.0", "book", [0.35, 0.30, 0.68, 0.72])
        coin = thing("2.1", "coin", [0.49, 0.49, 0.52, 0.52])  # right at the center, but 0.09% of the view
        self.view(book, coin)
        self.assertIs(self.seen.current(), book)
        self.view(coin)
        self.assertIs(self.seen.current(), coin)  # unless it's all there is

    def test_what_they_keep_pointing_at_beats_what_passes_by(self):
        bag = thing("2.0", "bag", [0.40, 0.38, 0.62, 0.66])        # held, a little off the middle
        bottle = thing("2.1", "bottle", [0.44, 0.30, 0.56, 0.70])  # passes right through the middle
        self.view(bag, seconds=1.5, steps=15)
        self.view(bag, bottle)
        self.assertIs(self.seen.current(), bag)
        self.view(bag, bottle, seconds=1.5, steps=15)  # it stays there: now they may mean either
        self.assertIs(self.seen.rival(self.seen.current()), bottle if self.seen.current() is bag else bag)

    def test_after_a_pan_the_new_object_wins_within_a_second(self):
        self.view(thing("2.0", "mug", [0.42, 0.40, 0.58, 0.60]), seconds=2.0, steps=20)
        mug = thing("2.0", "mug", [0.28, 0.40, 0.44, 0.60])     # still near the center, off to the left
        lamp = thing("2.1", "lamp", [0.42, 0.35, 0.58, 0.65])   # now in the middle
        self.view(mug, lamp, seconds=0.8, steps=8)
        self.assertIs(self.seen.current(), lamp)

    def test_two_side_by_side_are_a_toss_up(self):
        left = thing("2.0", "tumbler", [0.36, 0.38, 0.49, 0.70])
        right = thing("2.1", "tumbler", [0.51, 0.38, 0.64, 0.70])
        self.view(left, right)
        self.assertIsNotNone(self.seen.rival(self.seen.current()))


class FocusTest(unittest.TestCase):
    """What "this" is: a tap in the app, while it's recent and its object tracked; else what's in front."""

    def setUp(self):
        self.clock = Clock()
        self.seen = Seen(clock=self.clock)
        self.seen.update(objects(SOFA, MUG, LAMP))

    def tap(self, uid):
        self.seen.update(look(uid, "started"))
        self.seen.update(look(uid, "started", selected=True))

    def test_what_is_in_front(self):
        self.assertEqual(self.seen.focus(), (MUG, "in_front"))

    def test_a_tap_wins_over_what_is_in_front(self):
        self.tap("1.2")
        self.assertEqual(self.seen.focus(), (LAMP, "picked"))

    def test_a_tap_lasts_a_minute(self):
        self.tap("1.2")
        self.clock.now += seen_module.PICK_S + 1
        self.assertEqual(self.seen.focus(), (MUG, "in_front"))

    def test_a_tap_on_an_object_no_longer_tracked_doesnt_count(self):
        self.tap("1.2")
        self.seen.update(objects(SOFA, MUG))  # the lamp left scene memory
        self.assertEqual(self.seen.focus(), (MUG, "in_front"))

    def test_a_tap_from_before_the_agent_joined_isnt_this(self):  # the catch-up: its Look comes already selected
        self.seen.update(look("1.2", "results", selected=True))
        self.assertEqual((self.seen.picked, self.seen.focus()), (None, (MUG, "in_front")))

    def test_the_assistants_own_pick_isnt_a_tap(self):
        self.seen.agent_picked.add("1.2")
        self.tap("1.2")
        self.assertEqual((self.seen.picked, self.seen.focus()), (None, (MUG, "in_front")))

    def test_nothing(self):
        self.seen.update(objects(LAMP, VASE))
        self.assertEqual(self.seen.focus(), (None, "none"))

    def test_a_pick_by_voice_is_this_like_a_tap(self):
        self.seen.pick("1.2")
        self.assertEqual(self.seen.focus(), (LAMP, "picked"))
        self.clock.now += seen_module.PICK_S + 1
        self.assertEqual(self.seen.focus(), (MUG, "in_front"))


class AssistantTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.local = FakeLocalParticipant()
        self.room = FakeRoom(self.local, FakeParticipant("phone-1"))
        patcher = mock.patch.object(actions, "get_job_context", lambda: mock.Mock(room=self.room))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = mock.Mock()
        patcher = mock.patch.object(Assistant, "session", new_callable=mock.PropertyMock, return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seen = Seen()
        self.seen.update(objects(SOFA, MUG, LAMP))
        self.seen.update(LABEL)
        self.agent = Assistant(self.seen)

    async def asyncTearDown(self):
        await self.agent.on_exit()

    def rpcs(self, method):
        return [payload for _, m, payload in self.local.calls if m == method]


class GetCurrentObjectTest(AssistantTest):
    async def test_what_the_camera_is_on_and_its_prices(self):
        self.seen.update(look("1.1", "results"))
        data = app_data(await self.agent.get_current_object(None))
        self.assertEqual((data["name"], data["why"], data["where"], data["price_guess"], data["item"]),
                         ("white ceramic mug", "in_front", "center", "$8-15", "coffee mug"))
        self.assertEqual((data["best_match"]["store"], data["best_match"]["same_product"]), ("Target", True))
        self.assertEqual((data["dupes"][0]["store"], data["dupes"][0]["same_product"]), ("Amazon", False))
        self.assertEqual(data["price_check"], "That's a fair price.")
        self.assertEqual(self.local.calls, [])  # its Look is done: nothing to start

    async def test_the_tapped_item_and_what_is_in_front(self):
        self.seen.update(look("1.2", "results"))
        self.seen.update(look("1.2", "results", selected=True))
        data = app_data(await self.agent.get_current_object(None))
        self.assertEqual((data["name"], data["why"], data["where"], data["in_front"]),
                         ("coffee mug", "picked", "top right", "white ceramic mug"))

    async def test_prices_still_loading_answers_at_once_and_starts_the_search(self):
        self.seen.update(look("1.1", "described"))
        data = await asyncio.wait_for(self.agent.get_current_object(None), 1)  # no waiting on the stores
        data = app_data(data)
        self.assertEqual((data["item"], data["price_guess"], data["store_prices"]), ("coffee mug", "$8-15", "checking"))
        self.assertNotIn("best_match", data)
        await asyncio.sleep(0.05)
        self.assertEqual(self.rpcs("select"), [{"uid": "1.1"}])  # its Look runs now, as a tap would start it
        self.assertIn("1.1", self.seen.agent_picked)

    async def test_it_tells_the_prices_when_they_come_in(self):
        self.seen.update(look("1.1", "described"))
        with mock.patch.object(assistant, "POLL_S", 0.01):
            await self.agent.get_current_object(None)
            await asyncio.sleep(0.02)  # it sends its select
            self.seen.update(look("1.1", "results", selected=True))  # the backend's answer: picked, then priced
            await asyncio.sleep(0.1)
        self.session.generate_reply.assert_called_once()
        said = self.session.generate_reply.call_args.kwargs["instructions"]
        self.assertEqual(app_data(said)["best_match"]["title"], "Stoneware Mug 16oz")
        self.assertIsNone(self.seen.picked)  # the agent's select isn't the user's tap

    async def test_not_once_they_moved_on(self):
        self.seen.update(look("1.1", "described"))
        self.seen.update(objects(SOFA, MUG, LAMP, thing("1.5", "cup", [0.48, 0.45, 0.52, 0.55])))
        with mock.patch.object(assistant, "POLL_S", 0.01):
            await self.agent.get_current_object(None)  # the cup, now at the center
            self.seen.update(objects(SOFA, MUG, LAMP))
            await self.agent.get_current_object(None)  # the mug
            self.seen.update({**look("1.5", "results")})
            await asyncio.sleep(0.1)
        self.session.generate_reply.assert_not_called()  # the cup's prices came in, but they asked about the mug

    async def test_the_looks_brand_reaches_the_agent(self):
        m = look("1.1", "described")
        m["item"]["brand_guess"] = "Le Creuset"
        self.seen.update(m)
        self.assertEqual(app_data(await self.agent.get_current_object(None))["brand"], "Le Creuset")

    async def test_prices_failed(self):
        self.seen.update(look("1.1", "failed"))
        data = app_data(await self.agent.get_current_object(None))
        self.assertEqual(data["store_prices"], "not found")

    async def test_two_equally_in_front_ask_which(self):
        self.seen.update(objects(thing("2.0", "speaker", [0.36, 0.38, 0.49, 0.70]),
                                 thing("2.1", "charger", [0.51, 0.38, 0.64, 0.70])))
        data = app_data(await self.agent.get_current_object(None))
        self.assertEqual({data["tracked_as"], data["or_maybe"]}, {"speaker", "charger"})

    async def test_nothing_in_view(self):
        self.seen.update(objects(CHAIR))
        self.assertEqual(await self.agent.get_current_object(None), "Nothing is in view right now.")

    async def test_nothing_at_the_center_says_what_is_in_view(self):
        self.seen.update(objects(LAMP, VASE))
        data = app_data(await self.agent.get_current_object(None))
        self.assertEqual(data, {"at_center": "nothing", "in_view": [{"name": "lamp", "where": "top right"},
                                                                    {"name": "vase", "where": "left"}]})


class ActionToolsTest(AssistantTest):
    async def test_add_to_cart_adds_the_item_talked_about(self):
        self.seen.update(look("1.1", "results"))
        await self.agent.get_current_object(None)  # the mug
        self.seen.update(objects(SOFA, LAMP))      # then the camera moves on
        added = app_data(await self.agent.add_to_cart(None))
        self.assertEqual(added["added"]["title"], "Stoneware Mug 16oz")
        self.assertEqual(self.rpcs("action"), [{"name": "add_to_cart", "product": SEARCH["best_match"]}])

    async def test_add_a_cheaper_dupe(self):
        self.seen.update(look("1.1", "results"))
        await self.agent.get_current_object(None)
        await self.agent.add_to_cart(None, option="cheaper_dupe")
        self.assertEqual(self.rpcs("action")[0]["product"]["title"], "White Ceramic Mug")

    async def test_nothing_to_add_yet(self):
        self.seen.update(look("1.1", "described"))
        await self.agent.get_current_object(None)
        with self.assertRaisesRegex(ToolError, "store prices aren't in yet"):
            await self.agent.add_to_cart(None)
        with self.assertRaisesRegex(ToolError, "no used"):
            self.seen.update(look("1.1", "results"))
            await self.agent.add_to_cart(None, option="used")
        self.assertEqual(self.rpcs("action"), [])

    async def test_results_open_only_for_a_tapped_item(self):
        self.seen.update(look("1.1", "results"))
        await self.agent.get_current_object(None)
        with self.assertRaisesRegex(ToolError, "tap the white ceramic mug"):
            await self.agent.show_results(None)
        self.seen.update(look("1.1", "results", selected=True))  # the user taps it
        await self.agent.show_results(None, tab="dupes")
        self.assertEqual(self.rpcs("action"), [{"name": "show_results", "tab": "dupes"}])

    async def test_save_sends_its_look(self):
        self.seen.update(look("1.1", "results"))
        await self.agent.get_current_object(None)
        await self.agent.save_item(None)
        self.assertEqual(self.rpcs("action"), [{"name": "save_item", "capture_id": "l_1.1"}])

    async def test_the_apps_refusal_is_the_answer(self):
        self.local.reply = json.dumps({"ok": False, "detail": "That item isn't on screen anymore."})
        self.seen.update(look("1.1", "results"))
        await self.agent.get_current_object(None)
        with self.assertRaisesRegex(ToolError, "isn't on screen anymore"):
            await self.agent.save_item(None)

    async def test_cart_actions_without_an_item(self):
        await self.agent.undo_last(None)
        await self.agent.start_checkout(None)
        self.assertEqual(self.rpcs("action"), [{"name": "undo_last"}, {"name": "start_checkout"}])


class PickByVoiceTest(AssistantTest):
    async def test_whats_here_lists_the_tracked_items_with_their_ids(self):
        self.seen.update(objects(SOFA, MUG, LAMP, CHAIR))
        self.assertEqual(app_data(await self.agent.whats_here(None))["items"], [
            {"id": "1.0", "name": "sofa", "where": "center"}, {"id": "1.1", "name": "white ceramic mug", "where": "center"},
            {"id": "1.2", "name": "lamp", "where": "top right"}, {"id": "1.4", "name": "chair", "where": "out of view now"}])
        self.seen.update(objects())
        self.assertEqual(await self.agent.whats_here(None), "Nothing is tracked right now.")

    async def test_a_pick_opens_it_in_the_app_and_makes_it_this(self):
        self.seen.update(look("1.2", "results"))
        data = app_data(await self.agent.select_object(None, "1.2"))
        self.assertEqual(self.rpcs("select"), [{"uid": "1.2"}])  # the backend picks it: the viewer shows it
        self.assertEqual(self.rpcs("action"), [{"name": "select_item", "uid": "1.2"}])
        self.assertEqual((data["tracked_as"], data["why"], data["in_front"]), ("lamp", "picked", "white ceramic mug"))
        self.assertEqual(self.seen.focus(), (LAMP, "picked"))  # over the mug in front, from now on
        await self.agent.show_results(None, tab="dupes")  # the app shows it, so its results can open there
        self.assertEqual(self.rpcs("action")[-1], {"name": "show_results", "tab": "dupes"})

    async def test_an_app_that_cant_open_it_yet(self):
        self.local.reply = json.dumps({"ok": False, "detail": "Not in the app yet."})
        self.seen.update(look("1.2", "results"))
        data = app_data(await self.agent.select_object(None, "1.2"))
        self.assertEqual((data["why"], self.seen.focus()), ("picked", (LAMP, "picked")))  # still what "this" is
        with self.assertRaisesRegex(ToolError, "tap the"):  # but the app shows something else
            await self.agent.show_results(None)

    async def test_an_item_no_longer_tracked(self):
        with self.assertRaisesRegex(ToolError, "isn't tracked anymore"):
            await self.agent.select_object(None, "9.9")
        self.assertEqual(self.rpcs("action"), [])
