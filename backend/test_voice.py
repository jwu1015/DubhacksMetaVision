"""Tests for voice.py and POST /voice/token. No LiveKit, no Claude, no network: the main server's stream
events come from a local WebSocket server, its /look route from httpx.MockTransport, and the phone's RPC
from a stand-in room.

    python -m unittest test_voice -v      (from backend/)
"""
import asyncio
import json
import unittest
from unittest import mock

import httpx
import websockets
from fastapi.testclient import TestClient
from livekit import api, rtc
from livekit.agents import Agent, ChatContext, ModelSettings, ToolError, llm
from livekit.plugins import anthropic

import config
import main
import voice


def look(stage, capture_id="c_1", track_id=3, **extra):
    return {"type": "look", "stage": stage, "capture_id": capture_id, "track_id": track_id, **extra}


def product(title, store, price, savings=None, tier="new"):
    return {"title": title, "store": store, "price": price, "currency": "USD", "url": "https://example.com",
            "image_url": None, "source": "lens", "tier": tier, "source_rank": 0, "match_score": None,
            "savings_pct": savings, "match_reason": None}


SEARCH = {
    "capture_id": "c_1", "spoken_summary": "Wayfair has it for about 230 dollars.",
    "best_match": product("Walnut Tripod Floor Lamp with Linen Shade", "Wayfair", 229.99),
    "dupes": [product("Wood Tripod Floor Lamp", "Amazon", 119.99, 48, "dupe"),
              product("Tripod Floor Lamp, Walnut Finish " + "x" * 80, "Target", 89.99, 61, "dupe")],
    "used": [product("Vintage Walnut Tripod Floor Lamp", "eBay", 95.0, 59, "used")],
    "price_insight": {"typical_low": 71.2, "typical_high": 294.0, "median": 210.0, "sample_size": 14,
                      "best_match_verdict": "fair", "verdict_text": "Wayfair is within the typical $71–$294 range."},
    "timing_ms": {"total": 2400}, "item": None,
}
ITEM = {"category": "floor lamp", "description": "Tripod floor lamp with a cream drum shade."}
OBJECTS = {"type": "objects", "frame_idx": 9, "width": 504, "height": 896, "age_ms": 300, "objects": [
    {"id": 3, "label": "lamp", "score": 0.9, "box": {"x": 0.35, "y": 0.1, "w": 0.3, "h": 0.8}},
    {"id": 4, "label": "sofa", "score": 0.8, "box": {"x": 0.0, "y": 0.4, "w": 1.0, "h": 0.6}},
    {"id": 5, "label": "vase", "score": 0.7, "box": {"x": 0.8, "y": 0.8, "w": 0.1, "h": 0.1}}]}


NOW = 1792286014123  # ms: the scene memory's clock in these tests (voice.now_ms)


def setUpModule():
    patcher = mock.patch.object(voice, "now_ms", lambda: NOW)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def thing(id, label, x, y, w, h, ago=None, **fields):
    """A scene memory object: in view, or out of view and last seen ago seconds ago."""
    return {"id": id, "label": label, "score": 0.8, "box": {"x": x, "y": y, "w": w, "h": h},
            "in_view": ago is None, "centered": 0.5, "seconds_viewed": 2.0,
            "first_seen_ts": NOW - 14000, "last_seen_ts": NOW - (ago or 0) * 1000, **fields}


def scene(*objects) -> dict:
    return {"type": "objects", "frame_idx": 42, "width": 504, "height": 896, "age_ms": 310, "objects": list(objects)}


def ids(found: voice.Resolved) -> list[int]:
    return [o["id"] for o in found.found]


# The scene memory example from the handoff: a lamp on the right in view, a chair passed 5 s ago.
LAMP = thing(4, "lamp", 0.70, 0.30, 0.28, 0.30, centered=0.82, seconds_viewed=3.4)
CHAIR = thing(2, "chair", 0.05, 0.40, 0.30, 0.50, ago=5, centered=0.40, seconds_viewed=1.2)
LEFT_LAMP = thing(7, "lamp", 0.02, 0.30, 0.28, 0.30, centered=0.80)  # as far left of the center as LAMP is right


def picked() -> voice.Seen:
    """A Seen with the lamp picked, described and priced."""
    seen = voice.Seen()
    for ev in (OBJECTS, look("detected", label="lamp", quick_phrase="Lamp. Finding prices."),
               look("described", item=ITEM), look("results", search=SEARCH)):
        seen.update(ev)
    return seen


class SeenTest(unittest.TestCase):
    def test_a_look_is_followed_and_narrated(self):
        seen = voice.Seen()
        self.assertIsNone(seen.update(OBJECTS))
        self.assertEqual(seen.update(look("detected", label="lamp", quick_phrase="Lamp. Finding prices.")),
                         "Lamp. Finding prices.")
        self.assertIsNone(seen.update(look("described", item=ITEM)))  # nothing to say: the name shows up in replies
        self.assertEqual(seen.update(look("results", search=SEARCH)), SEARCH["spoken_summary"])
        self.assertEqual((seen.look["item"], seen.look["search"]), (ITEM, SEARCH))
        self.assertEqual(len(seen.objects), 3)
        self.assertIsNone(seen.update({"type": "status", "tracking": "stopped"}))
        self.assertEqual(seen.objects, [])

    def test_failed_and_nothing_are_said_too(self):
        seen = voice.Seen()
        seen.update(look("detected", quick_phrase="Lamp. Finding prices."))
        self.assertEqual(seen.update(look("failed", message="Couldn't get prices for that one.")),
                         "Couldn't get prices for that one.")
        self.assertEqual(seen.update(look("nothing", "c_2", None, quick_phrase="I couldn't spot a product there.")),
                         "I couldn't spot a product there.")

    def test_a_replayed_look_is_ignored(self):  # it's from before this conversation, maybe long before
        seen = voice.Seen()
        for ev in (look("detected", quick_phrase="Lamp. Finding prices."), look("results", search=SEARCH)):
            self.assertIsNone(seen.update({**ev, "replay": True}))
        self.assertIsNone(seen.look)

    def test_late_events_of_an_older_look_are_ignored(self):
        seen = voice.Seen()
        seen.update(look("detected", "c_old"))
        seen.update(look("detected", "c_new", quick_phrase="Sofa. Finding prices."))
        self.assertIsNone(seen.update(look("results", "c_old", search=SEARCH)))
        self.assertNotIn("search", seen.look)
        self.assertEqual(seen.look["capture_id"], "c_new")

    def test_products(self):
        seen = picked()
        self.assertEqual(seen.product("best_match", 1)["store"], "Wayfair")
        self.assertEqual(seen.product("dupe", 2)["store"], "Target")
        self.assertEqual(seen.product("used", 1)["store"], "eBay")
        for option, number, why in (("dupe", 3, "only 2"), ("used", 0, "only 1"), ("best_match", 2, "only 1")):
            with self.assertRaisesRegex(ToolError, why):
                seen.product(option, number)
        seen.update(look("detected", "c_2"))
        with self.assertRaisesRegex(ToolError, "still loading"):
            seen.product("best_match", 1)
        seen.update(look("nothing", "c_3", None))
        with self.assertRaisesRegex(ToolError, "Nothing is picked"):
            seen.product("best_match", 1)

    def test_note(self):
        seen = voice.Seen()
        seen.update(OBJECTS)
        self.assertEqual(seen.note(), "No item is picked. In view: lamp, sofa, vase.")
        seen.update(look("detected", label="lamp"))
        self.assertIn("Current item: lamp.\nIts prices are still loading.", seen.note())
        note = picked().note()
        self.assertTrue(note.startswith("Current item: floor lamp.\nPrice check: Wayfair is within"), note)
        self.assertIn("<app_data>\nDescription: Tripod floor lamp", note)  # store and image text stay in app_data
        self.assertIn("Best match: Walnut Tripod Floor Lamp with Linen Shade, Wayfair, $230", note)
        cut = SEARCH["dupes"][1]["title"][:voice.TITLE_CHARS]
        self.assertIn(f"Dupes: 1. Wood Tripod Floor Lamp, Amazon, $120, save 48%; 2. {cut}, Target, $89.99, save 61%",
                      note)
        self.assertIn("Used: 1. Vintage Walnut Tripod Floor Lamp, eBay, $95.00, save 59%", note)
        self.assertTrue(note.endswith("</app_data>"))
        failed = picked()
        failed.update(look("failed", message="nope"))
        self.assertIn("Its prices failed to load.", failed.note())

    def test_the_note_names_what_was_seen_recently(self):
        seen = voice.Seen()
        seen.update(scene(LAMP, CHAIR, thing(9, "rug", 0, 0.8, 1, 0.2, ago=11.6)))
        self.assertEqual(seen.note(),
                         "No item is picked. In view: lamp. Recently seen: chair (5 s ago), rug (12 s ago).")
        seen.update(scene(LAMP, LEFT_LAMP, CHAIR))
        self.assertEqual(seen.note(), "No item is picked. In view: lamp (2). Recently seen: chair (5 s ago).")
        seen.update(scene(CHAIR))
        self.assertEqual(seen.note(), "No item is picked. In view: nothing tracked. Recently seen: chair (5 s ago).")

    def test_a_reset_scene_memory_forgets_its_ids(self):  # a mode switch or a pause: ids start over
        seen = picked()
        self.assertEqual(seen.described, {3: "floor lamp Tripod floor lamp with a cream drum shade."})
        seen.ranked, seen.tried, seen.asked = [3, 4], [3], []
        seen.update(scene())
        self.assertEqual((seen.objects, seen.described, seen.ranked, seen.tried), ([], {}, [], []))
        self.assertIsNone(seen.look["track_id"])  # a new object 3 isn't the current item
        self.assertEqual(seen.look["item"], ITEM)  # which is still picked


class ResolveTest(unittest.TestCase):
    def assertShort(self, text):
        self.assertLessEqual(len(text.split()), 12, text)

    def test_the_label_and_what_people_call_it(self):
        labels = "sofa,chair,table,lamp,rug,bed,shelf,mirror,wall art,vase,pillow,blanket".split(",")
        objects = [thing(i, label, 0.02 + 0.08 * i, 0.05, 0.05, 0.05) for i, label in enumerate(labels)]
        for said, label in (("lamp", "lamp"), ("lamps", "lamp"), ("couch", "sofa"), ("the couches", "sofa"),
                            ("armchair", "chair"), ("carpet", "rug"), ("the light", "lamp"), ("painting", "wall art"),
                            ("picture", "wall art"), ("poster", "wall art"), ("wall art", "wall art"),
                            ("bookcase", "shelf"), ("bookshelf", "shelf"), ("cushion", "pillow"), ("throw", "blanket"),
                            ("desk", "table"), ("coffee table", "table"), ("floor lamp", "lamp"),
                            ("throw pillow", "pillow"), ("light blue chair", "chair"),
                            ("lamp next to the couch", "lamp"),
                            ("that mirror over there", "mirror"), ("the bed I just passed", "bed")):
            found = voice.resolve(objects, said)
            self.assertEqual((found.found[0]["label"], found.say), (label, None), said)
        # A tracker label of another kind counts as its kind: a floor lamp is a lamp.
        objects = [thing(1, "floor lamp", 0.1, 0.1, 0.1, 0.1), thing(2, "couch", 0.8, 0.8, 0.1, 0.1)]
        for said, picked_id in (("light", 1), ("lamp", 1), ("floor lamp", 1), ("sofa", 2), ("couch", 2)):
            self.assertEqual(ids(voice.resolve(objects, said)), [picked_id], said)

    def test_that_is_what_is_at_the_center_of_the_view(self):
        objects = OBJECTS["objects"]  # an event from before scene memory: no in_view, centered or times
        for said in ("", "this", "that one", "what's this thing"):  # the lamp and the sofa cover the center:
            found = voice.resolve(objects, said)                     # the lamp, in front
            self.assertEqual((ids(found)[0], found.say), (3, None), said)
        self.assertEqual(ids(voice.resolve(objects[2:], "")), [5])  # nothing covers it: the nearest
        self.assertEqual(ids(voice.resolve(objects, "sofa")), [4])
        found = voice.resolve([CHAIR], "")  # only what's in view counts
        self.assertEqual((found.found, found.say), ([], "Nothing's in view right now."))

    def test_an_out_of_view_chair(self):
        for said in ("chair", "what about the chair I just passed", "the one I just passed", "the armchair before"):
            found = voice.resolve([LAMP, CHAIR], said)
            self.assertEqual((ids(found)[0], found.say), (2, None), said)
        self.assertEqual(ids(voice.resolve([LAMP, CHAIR], "lamp")), [4])
        # A chair in view, at the edge: "chair" is that one, "the chair I passed" the other.
        edge = thing(8, "chair", 0.0, 0.4, 0.1, 0.2, centered=0.2)
        self.assertEqual(ids(voice.resolve([edge, LAMP, CHAIR], "chair")), [8, 2])
        self.assertEqual(ids(voice.resolve([edge, LAMP, CHAIR], "the chair I passed"))[0], 2)

    def test_two_lamps_ask_which(self):
        found = voice.resolve([LAMP, LEFT_LAMP, CHAIR], "how much is that lamp")
        self.assertEqual((ids(found), found.say), ([4, 7], "The lamp on the left or the right?"))
        for answer, picked_id in (("the left one", 7), ("left", 7), ("the right one", 4)):
            found = voice.resolve([LAMP, LEFT_LAMP, CHAIR], answer, among=[4, 7])
            self.assertEqual((ids(found)[0], found.say), (picked_id, None), answer)
        self.assertEqual(ids(voice.resolve([LAMP, LEFT_LAMP, CHAIR], "lamp on the left"))[0], 7)
        self.assertEqual(ids(voice.resolve([LAMP, LEFT_LAMP, CHAIR], "the lamp right there"))[0], 4)  # not "right"

        high, low = thing(5, "lamp", 0.4, 0.0, 0.2, 0.3), thing(6, "lamp", 0.42, 0.7, 0.2, 0.3)
        self.assertEqual(voice.resolve([high, low], "lamp").say, "The higher lamp or the lower one?")
        self.assertEqual(ids(voice.resolve([high, low], "the bottom one", among=[5, 6]))[0], 6)

        passed = thing(9, "lamp", 0.4, 0.3, 0.2, 0.3, ago=1)  # just out of view, against one at the edge in view
        edge = thing(10, "lamp", 0.95, 0.3, 0.05, 0.3, centered=0.0)
        found = voice.resolve([edge, passed], "lamp")
        self.assertEqual((ids(found), found.say), ([10, 9], "The lamp in front of you, or the one you passed?"))
        self.assertEqual(ids(voice.resolve([edge, passed], "the one I passed", among=[10, 9]))[0], 9)
        self.assertEqual(ids(voice.resolve([edge, passed], "the one in front", among=[10, 9]))[0], 10)

        older = thing(11, "lamp", 0.4, 0.3, 0.2, 0.3, ago=2.5)
        found = voice.resolve([LAMP, passed, older], "lamp", exclude=[4])
        self.assertEqual((ids(found), found.say), ([9, 11], "The lamp you just passed, or the one before?"))
        self.assertEqual(ids(voice.resolve([LAMP, passed, older], "the one before", among=[9, 11]))[0], 11)
        self.assertEqual(ids(voice.resolve([LAMP, passed, older], "the lamp I just passed", exclude=[4]))[0], 9)

        vase = thing(12, "vase", 0.58, 0.45, 0.1, 0.1, centered=0.82)  # "that": two things as near the center
        self.assertEqual(voice.resolve([LAMP, vase, LEFT_LAMP], "").say, "The vase or the lamp?")
        for found in (voice.resolve([LAMP, LEFT_LAMP], "lamp"), voice.resolve([LAMP, vase], "")):
            self.assertShort(found.say)

    def test_no_question_when_one_is_clearly_it(self):
        center = thing(5, "lamp", 0.4, 0.2, 0.2, 0.5, centered=0.3)  # covering the center now
        self.assertEqual(voice.resolve([LAMP, center], "lamp"), voice.Resolved([center, LAMP]))
        far = thing(6, "lamp", 0.0, 0.0, 0.1, 0.1, centered=0.1)
        self.assertEqual(voice.resolve([LAMP, far], "lamp"), voice.Resolved([LAMP, far]))
        sofa = thing(7, "sofa", 0.0, 0.3, 1.0, 0.7, centered=1.0)  # the lamp in front of it covers the center too
        self.assertEqual(voice.resolve([sofa, center], ""), voice.Resolved([center, sofa]))
        stale = thing(8, "lamp", 0.4, 0.3, 0.2, 0.3, ago=4)
        self.assertEqual(voice.resolve([LAMP, stale], "lamp").found, [LAMP, stale])

    def test_the_other_one(self):
        found = voice.resolve([LAMP, LEFT_LAMP, CHAIR], "lamp", exclude=[7])
        self.assertEqual((ids(found), found.say), ([4], None))
        found = voice.resolve([LAMP, LEFT_LAMP, CHAIR], "lamp", exclude=[7, 4])
        self.assertEqual((found.found, found.say), ([], "That's the only lamp I've seen."))
        self.assertEqual(voice.resolve([LAMP], "", exclude=[4]).say, "Nothing else is in view.")

    def test_what_it_looks_like_from_claudes_descriptions(self):
        described = {4: "table lamp Green ceramic lamp with a white shade.", 7: "floor lamp Brass arc floor lamp."}
        for said, picked_id in (("green lamp", 4), ("the floor lamp", 7), ("brass one", 7), ("the green one", 4)):
            found = voice.resolve([LAMP, LEFT_LAMP], said, described=described)
            self.assertEqual((ids(found)[0], found.say), (picked_id, None), said)
        self.assertIsNotNone(voice.resolve([LAMP, LEFT_LAMP], "red lamp", described=described).say)  # unknown: ask
        self.assertIsNotNone(voice.resolve([LAMP, LEFT_LAMP], "green lamp").say)  # nothing to check it against

    def test_nothing_matches(self):
        for objects, said, say in (([LAMP, CHAIR], "mirror", "No mirror in view or seen recently."),
                                   ([LAMP], "the painting", "No painting in view or seen recently."),
                                   ([LAMP], "couch", "No couch in view or seen recently."),
                                   ([], "", "Nothing's in view right now."),
                                   ([], "lamp", "No lamp in view or seen recently.")):
            found = voice.resolve(objects, said)
            self.assertEqual((found.found, found.say), ([], say), said)
            self.assertShort(say)


class FollowTest(unittest.IsolatedAsyncioTestCase):
    async def test_follows_the_stream_and_says_only_what_happens_live(self):
        events = [{"type": "status", "tracking": "ok"}, OBJECTS,
                  {**look("detected", "c_0", quick_phrase="Sofa. Finding prices."), "replay": True},
                  "not json", look("detected", quick_phrase="Lamp. Finding prices."), look("results", search=SEARCH)]

        async def serve(ws):
            assert ws.request.path == "/stream/events"
            for ev in events:
                await ws.send(ev if isinstance(ev, str) else json.dumps(ev))
            await ws.wait_closed()  # stay open, like the real stream

        said: list[str] = []
        async with websockets.serve(serve, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with mock.patch.object(voice, "BACKEND", f"http://127.0.0.1:{port}"):
                seen = voice.Seen()
                follower = asyncio.create_task(seen.follow(said.append))
                async with asyncio.timeout(5):
                    while len(said) < 2:
                        await asyncio.sleep(0.02)
                follower.cancel()
        self.assertEqual(said, ["Lamp. Finding prices.", SEARCH["spoken_summary"]])
        self.assertEqual((seen.look["capture_id"], len(seen.objects)), ("c_1", 3))


class FakeLocalParticipant:
    def __init__(self, reply="{\"ok\": true}"):
        self.reply, self.calls = reply, []

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout=None):
        self.calls.append((destination_identity, method, json.loads(payload)))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeParticipant:
    def __init__(self, identity, kind):
        self.identity, self.kind = identity, kind


class FakeRoom:
    def __init__(self, local, *participants):
        self.local_participant = local
        self.remote_participants = {p.identity: p for p in participants}


class ToolTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.posts: list[str] = []
        self.look_status = 200

        def backend(request: httpx.Request):
            if isinstance(self.look_status, Exception):
                raise self.look_status
            self.posts.append(request.url.path)
            return httpx.Response(self.look_status, json={})
        self.backend = httpx.AsyncClient(transport=httpx.MockTransport(backend), base_url="http://backend")
        self.local = FakeLocalParticipant()
        self.room = FakeRoom(self.local, FakeParticipant("agent-x", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT),
                             FakeParticipant("phone-1", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD))
        patcher = mock.patch.object(voice, "get_job_context", lambda: mock.Mock(room=self.room))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        await self.backend.aclose()

    def agent(self, seen: voice.Seen) -> voice.Spotted:
        return voice.Spotted(seen, self.backend)

    async def test_look_at_looks_at_what_they_mean(self):
        seen = voice.Seen()
        self.assertEqual(await self.agent(seen).look_at(None, ""), 'Reply with just: "Nothing\'s in view right now."')
        seen.update(OBJECTS)  # an event from before scene memory
        self.assertIsNone(await self.agent(seen).look_at(None, ""))  # the app says what it found
        self.assertIsNone(await self.agent(seen).look_at(None, "couch"))
        self.assertEqual(self.posts, ["/look/3", "/look/4"])
        seen.update(scene(LAMP, CHAIR))
        self.assertIsNone(await self.agent(seen).look_at(None, "the chair I just passed"))  # out of view
        self.assertEqual(self.posts[-1], "/look/2")
        self.assertEqual(await self.agent(seen).look_at(None, "mirror"),
                         'Reply with just: "No mirror in view or seen recently."')
        self.assertEqual(len(self.posts), 3)

    async def test_when_the_look_fails(self):
        seen = voice.Seen()
        seen.update(scene(LAMP, CHAIR))
        self.look_status = 404  # it left the scene memory
        with self.assertRaisesRegex(ToolError, "Lost track"):
            await self.agent(seen).look_at(None, "chair")
        self.look_status = 500
        with self.assertRaisesRegex(ToolError, "couldn't look"):
            await self.agent(seen).look_at(None, "lamp")
        self.look_status = httpx.ConnectError("refused")
        with self.assertRaisesRegex(ToolError, "couldn't look"):
            await self.agent(seen).look_at(None, "lamp")

    async def test_two_lamps_ask_which_then_the_answer_picks(self):
        seen = voice.Seen()
        seen.update(scene(LAMP, LEFT_LAMP, CHAIR))
        agent = self.agent(seen)
        self.assertEqual(await agent.look_at(None, "lamp"), "The lamp on the left or the right?")
        self.assertEqual((self.posts, seen.asked), ([], [4, 7]))
        self.assertIsNone(await agent.look_at(None, "the left one"))
        self.assertEqual((self.posts, seen.asked), (["/look/7"], []))

    async def test_the_item_just_picked_starts_nothing(self):
        seen = picked()
        for target in ("", "lamp", "that floor lamp"):
            self.assertIn("already the current item", await self.agent(seen).look_at(None, target))
        self.assertEqual(self.posts, [])

    async def test_the_other_one(self):
        seen = voice.Seen()
        seen.update(scene(LAMP, LEFT_LAMP, CHAIR))
        agent = self.agent(seen)

        async def say(tool, *args, track_id):  # then the app picks it, as the main server's events say
            self.assertIsNone(await tool(None, *args))
            self.assertEqual(self.posts[-1], f"/look/{track_id}")
            seen.update(look("detected", f"c_{len(self.posts)}", track_id, label="lamp"))
        await say(agent.look_at, "lamp on the right", track_id=4)
        await say(agent.look_at_other, track_id=7)
        await say(agent.look_at_other, track_id=4)  # they've both been tried: around again
        await say(agent.look_at, "no, the other one", track_id=7)
        await say(agent.look_at, "the other lamp", track_id=4)

        seen.update(look("detected", "c_phone", 7, label="lamp"))  # tapped on the phone
        await say(agent.look_at_other, track_id=4)

        seen.update(scene(LAMP, CHAIR))
        nothing = 'Reply with just: "There\'s no other one in view or seen recently."'
        self.assertEqual(await agent.look_at_other(None), nothing)
        self.assertEqual(await self.agent(voice.Seen()).look_at_other(None), nothing)

    async def test_actions_go_to_the_phone(self):
        agent = self.agent(picked())
        self.assertIn("Target's $89.99", await agent.add_to_cart(None, "dupe", 2))
        await agent.save_item(None)
        await agent.show_options(None, "used")
        await agent.undo(None)
        self.assertIn("Face ID", await agent.checkout(None))
        sent = [(who, method, payload["name"]) for who, method, payload in self.local.calls]
        self.assertEqual(sent, [("phone-1", "action", name) for name in
                                ("add_to_cart", "save_item", "show_results", "undo_last", "start_checkout")])
        add, _, show = (payload for _, _, payload in self.local.calls[:3])
        self.assertEqual((add["capture_id"], add["product"]), ("c_1", SEARCH["dupes"][1]))
        self.assertEqual((show["capture_id"], show["tab"]), ("c_1", "used"))

    async def test_when_the_phone_cant(self):
        agent = self.agent(picked())
        self.local.reply = json.dumps({"ok": False, "detail": "The cart is full."})
        with self.assertRaisesRegex(ToolError, "The cart is full."):
            await agent.add_to_cart(None, "best_match")
        self.local.reply = rtc.RpcError(rtc.RpcError.ErrorCode.UNSUPPORTED_METHOD, "no such method")
        with self.assertRaisesRegex(ToolError, "couldn't do that"):
            await agent.undo(None)
        self.room.remote_participants.pop("phone-1")  # only the agent is left
        with self.assertRaisesRegex(ToolError, "isn't connected"):
            await agent.checkout(None)
        with self.assertRaisesRegex(ToolError, "Nothing is picked"):
            await self.agent(voice.Seen()).save_item(None)

    async def test_every_claude_call_gets_the_note(self):
        ctx = ChatContext.empty()
        ctx.add_message(role="user", content="Is that a good price?")
        sent = []

        async def default_llm_node(agent, chat_ctx, tools, model_settings):
            sent.append(chat_ctx)
            yield "Fair, about 230 dollars."
        with mock.patch.object(Agent.default, "llm_node", default_llm_node):
            chunks = [c async for c in self.agent(picked()).llm_node(ctx, [], ModelSettings())]
        self.assertEqual(chunks, ["Fair, about 230 dollars."])
        note = sent[0].items[-1]
        self.assertEqual((note.role, sent[0].items[-2].text_content), ("system", "Is that a good price?"))
        self.assertIn("Price check: Wayfair is within the typical", note.text_content)
        self.assertEqual(len(ctx.items), 1)  # the session's own history doesn't keep old notes

    async def test_what_claude_says_before_a_tool_is_dropped(self):
        def text(s):
            return llm.ChatChunk(id="m", delta=llm.ChoiceDelta(role="assistant", content=s))
        call = llm.ChatChunk(id="m", delta=llm.ChoiceDelta(role="assistant", tool_calls=[
            llm.FunctionToolCall(name="add_to_cart", arguments='{"option": "dupe", "number": 2}', call_id="c")]))
        usage = llm.ChatChunk(id="m", usage=llm.CompletionUsage(completion_tokens=9, prompt_tokens=900, total_tokens=909))

        async def default_llm_node(agent, chat_ctx, tools, model_settings):
            for chunk in (text("The Target one's cheapest. "), text("I'll add it."), call, usage):
                yield chunk
        with mock.patch.object(Agent.default, "llm_node", default_llm_node):
            chunks = [c async for c in self.agent(picked()).llm_node(ChatContext.empty(), [], ModelSettings())]
        self.assertEqual(chunks, [call, usage])

        async def answer(agent, chat_ctx, tools, model_settings):
            for chunk in (text("Fair price, "), usage, text("most sell for 70 to 290.")):
                yield chunk
        with mock.patch.object(Agent.default, "llm_node", answer):
            chunks = [c async for c in self.agent(picked()).llm_node(ChatContext.empty(), [], ModelSettings())]
        self.assertEqual("".join(c.delta.content for c in chunks if c.delta), "Fair price, most sell for 70 to 290.")


class ClaudeTest(unittest.TestCase):
    def test_thinking_off_and_low_effort(self):
        llm = voice.Claude(model=voice.MODEL, api_key="test")
        with mock.patch.object(anthropic.LLM, "chat", return_value="stream") as chat:
            self.assertEqual(llm.chat(chat_ctx=ChatContext.empty()), "stream")
            self.assertEqual(chat.call_args.kwargs["extra_kwargs"],
                             {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}})
            llm.chat(chat_ctx=ChatContext.empty(), extra_kwargs={"metadata": {"user_id": "u"},
                                                                  "output_config": {"effort": "medium"}})
            self.assertEqual(chat.call_args.kwargs["extra_kwargs"], {
                "metadata": {"user_id": "u"}, "output_config": {"effort": "medium"}, "thinking": {"type": "disabled"}})


class TokenRouteTest(unittest.TestCase):
    KEY, SECRET = "APItestkey", "test-secret-" + "s" * 32

    def setUp(self):
        self.client = TestClient(main.app)  # no lifespan: no warm-up, no segmentation server

    def settings(self, url="wss://test.livekit.cloud"):
        return mock.patch.multiple(config, LIVEKIT_URL=url, LIVEKIT_API_KEY=self.KEY, LIVEKIT_API_SECRET=self.SECRET)

    def test_a_token_for_a_new_room_with_the_agent_in_it(self):
        with self.settings():
            r = self.client.post("/voice/token", json={"participant_identity": "phone-shiven", "room_name": "theirs"})
            other = self.client.post("/voice/token").json()
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(set(body), {"server_url", "participant_token"})
        self.assertEqual(body["server_url"], "wss://test.livekit.cloud")
        claims = api.TokenVerifier(self.KEY, self.SECRET).verify(body["participant_token"])
        self.assertEqual(claims.identity, "phone-shiven")
        self.assertTrue(claims.video.room_join)
        self.assertTrue(claims.video.room.startswith("spotted-"))  # our room, not the one asked for
        self.assertEqual([a.agent_name for a in claims.room_config.agents], [config.VOICE_AGENT])
        other_claims = api.TokenVerifier(self.KEY, self.SECRET).verify(other["participant_token"])
        self.assertNotEqual(other_claims.video.room, claims.video.room)  # a new room each time: the agent comes
        self.assertTrue(other_claims.identity.startswith("phone-"))

    def test_not_set_up(self):
        with self.settings(url=""):
            r = self.client.post("/voice/token")
        self.assertEqual(r.status_code, 503)
        self.assertIn("LIVEKIT_URL", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
