"""Tests for the voice groundwork (voice/) and POST /voice/token. No LiveKit, no Claude, no network: the main server's stream
events come from a local WebSocket server, its /look route from httpx.MockTransport, and the phone's RPC
from a stand-in room.
"""
import asyncio
import json
import unittest
from unittest import mock

import websockets
from fastapi.testclient import TestClient
from livekit import api, rtc
from livekit.agents import ToolError

from fakes.livekit import FakeLocalParticipant, FakeParticipant, FakeRoom
from storefront import app as main
from storefront import config
from storefront.voice import actions, resolve
from storefront.voice import seen as seen_module
from storefront.voice.seen import Seen


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


NOW = 1792286014123  # ms: the scene memory's clock in these tests (resolve.now_ms)


def setUpModule():
    patcher = mock.patch.object(resolve, "now_ms", lambda: NOW)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def thing(id, label, x, y, w, h, ago=None, **fields):
    """A scene memory object: in view, or out of view and last seen ago seconds ago."""
    return {"id": id, "label": label, "score": 0.8, "box": {"x": x, "y": y, "w": w, "h": h},
            "in_view": ago is None, "centered": 0.5, "seconds_viewed": 2.0,
            "first_seen_ts": NOW - 14000, "last_seen_ts": NOW - (ago or 0) * 1000, **fields}


def scene(*objects) -> dict:
    return {"type": "objects", "frame_idx": 42, "width": 504, "height": 896, "age_ms": 310, "objects": list(objects)}


def ids(found: resolve.Resolved) -> list[int]:
    return [o["id"] for o in found.found]


# The scene memory example from the handoff: a lamp on the right in view, a chair passed 5 s ago.
LAMP = thing(4, "lamp", 0.70, 0.30, 0.28, 0.30, centered=0.82, seconds_viewed=3.4)
CHAIR = thing(2, "chair", 0.05, 0.40, 0.30, 0.50, ago=5, centered=0.40, seconds_viewed=1.2)
LEFT_LAMP = thing(7, "lamp", 0.02, 0.30, 0.28, 0.30, centered=0.80)  # as far left of the center as LAMP is right


def picked() -> Seen:
    """A Seen with the lamp picked, described and priced."""
    seen = Seen()
    for ev in (OBJECTS, look("detected", label="lamp", quick_phrase="Lamp. Finding prices."),
               look("described", item=ITEM), look("results", search=SEARCH)):
        seen.update(ev)
    return seen


class SeenTest(unittest.TestCase):
    def test_a_look_is_followed_and_narrated(self):
        seen = Seen()
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
        seen = Seen()
        seen.update(look("detected", quick_phrase="Lamp. Finding prices."))
        self.assertEqual(seen.update(look("failed", message="Couldn't get prices for that one.")),
                         "Couldn't get prices for that one.")
        self.assertEqual(seen.update(look("nothing", "c_2", None, quick_phrase="I couldn't spot a product there.")),
                         "I couldn't spot a product there.")

    def test_a_replayed_look_is_ignored(self):  # it's from before this conversation, maybe long before
        seen = Seen()
        for ev in (look("detected", quick_phrase="Lamp. Finding prices."), look("results", search=SEARCH)):
            self.assertIsNone(seen.update({**ev, "replay": True}))
        self.assertIsNone(seen.look)

    def test_late_events_of_an_older_look_are_ignored(self):
        seen = Seen()
        seen.update(look("detected", "c_old"))
        seen.update(look("detected", "c_new", quick_phrase="Sofa. Finding prices."))
        self.assertIsNone(seen.update(look("results", "c_old", search=SEARCH)))
        self.assertNotIn("search", seen.look)
        self.assertEqual(seen.look["capture_id"], "c_new")

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
            found = resolve.resolve(objects, said)
            self.assertEqual((found.found[0]["label"], found.say), (label, None), said)
        # A tracker label of another kind counts as its kind: a floor lamp is a lamp.
        objects = [thing(1, "floor lamp", 0.1, 0.1, 0.1, 0.1), thing(2, "couch", 0.8, 0.8, 0.1, 0.1)]
        for said, picked_id in (("light", 1), ("lamp", 1), ("floor lamp", 1), ("sofa", 2), ("couch", 2)):
            self.assertEqual(ids(resolve.resolve(objects, said)), [picked_id], said)

    def test_that_is_what_is_at_the_center_of_the_view(self):
        objects = OBJECTS["objects"]  # an event from before scene memory: no in_view, centered or times
        for said in ("", "this", "that one", "what's this thing"):  # the lamp and the sofa cover the center:
            found = resolve.resolve(objects, said)                     # the lamp, in front
            self.assertEqual((ids(found)[0], found.say), (3, None), said)
        self.assertEqual(ids(resolve.resolve(objects[2:], "")), [5])  # nothing covers it: the nearest
        self.assertEqual(ids(resolve.resolve(objects, "sofa")), [4])
        found = resolve.resolve([CHAIR], "")  # only what's in view counts
        self.assertEqual((found.found, found.say), ([], "Nothing's in view right now."))

    def test_an_out_of_view_chair(self):
        for said in ("chair", "what about the chair I just passed", "the one I just passed", "the armchair before"):
            found = resolve.resolve([LAMP, CHAIR], said)
            self.assertEqual((ids(found)[0], found.say), (2, None), said)
        self.assertEqual(ids(resolve.resolve([LAMP, CHAIR], "lamp")), [4])
        # A chair in view, at the edge: "chair" is that one, "the chair I passed" the other.
        edge = thing(8, "chair", 0.0, 0.4, 0.1, 0.2, centered=0.2)
        self.assertEqual(ids(resolve.resolve([edge, LAMP, CHAIR], "chair")), [8, 2])
        self.assertEqual(ids(resolve.resolve([edge, LAMP, CHAIR], "the chair I passed"))[0], 2)

    def test_two_lamps_ask_which(self):
        found = resolve.resolve([LAMP, LEFT_LAMP, CHAIR], "how much is that lamp")
        self.assertEqual((ids(found), found.say), ([4, 7], "The lamp on the left or the right?"))
        for answer, picked_id in (("the left one", 7), ("left", 7), ("the right one", 4)):
            found = resolve.resolve([LAMP, LEFT_LAMP, CHAIR], answer, among=[4, 7])
            self.assertEqual((ids(found)[0], found.say), (picked_id, None), answer)
        self.assertEqual(ids(resolve.resolve([LAMP, LEFT_LAMP, CHAIR], "lamp on the left"))[0], 7)
        self.assertEqual(ids(resolve.resolve([LAMP, LEFT_LAMP, CHAIR], "the lamp right there"))[0], 4)  # not "right"

        high, low = thing(5, "lamp", 0.4, 0.0, 0.2, 0.3), thing(6, "lamp", 0.42, 0.7, 0.2, 0.3)
        self.assertEqual(resolve.resolve([high, low], "lamp").say, "The higher lamp or the lower one?")
        self.assertEqual(ids(resolve.resolve([high, low], "the bottom one", among=[5, 6]))[0], 6)

        passed = thing(9, "lamp", 0.4, 0.3, 0.2, 0.3, ago=1)  # just out of view, against one at the edge in view
        edge = thing(10, "lamp", 0.95, 0.3, 0.05, 0.3, centered=0.0)
        found = resolve.resolve([edge, passed], "lamp")
        self.assertEqual((ids(found), found.say), ([10, 9], "The lamp in front of you, or the one you passed?"))
        self.assertEqual(ids(resolve.resolve([edge, passed], "the one I passed", among=[10, 9]))[0], 9)
        self.assertEqual(ids(resolve.resolve([edge, passed], "the one in front", among=[10, 9]))[0], 10)

        older = thing(11, "lamp", 0.4, 0.3, 0.2, 0.3, ago=2.5)
        found = resolve.resolve([LAMP, passed, older], "lamp", exclude=[4])
        self.assertEqual((ids(found), found.say), ([9, 11], "The lamp you just passed, or the one before?"))
        self.assertEqual(ids(resolve.resolve([LAMP, passed, older], "the one before", among=[9, 11]))[0], 11)
        self.assertEqual(ids(resolve.resolve([LAMP, passed, older], "the lamp I just passed", exclude=[4]))[0], 9)

        vase = thing(12, "vase", 0.58, 0.45, 0.1, 0.1, centered=0.82)  # "that": two things as near the center
        self.assertEqual(resolve.resolve([LAMP, vase, LEFT_LAMP], "").say, "The vase or the lamp?")
        for found in (resolve.resolve([LAMP, LEFT_LAMP], "lamp"), resolve.resolve([LAMP, vase], "")):
            self.assertShort(found.say)

    def test_no_question_when_one_is_clearly_it(self):
        center = thing(5, "lamp", 0.4, 0.2, 0.2, 0.5, centered=0.3)  # covering the center now
        self.assertEqual(resolve.resolve([LAMP, center], "lamp"), resolve.Resolved([center, LAMP]))
        far = thing(6, "lamp", 0.0, 0.0, 0.1, 0.1, centered=0.1)
        self.assertEqual(resolve.resolve([LAMP, far], "lamp"), resolve.Resolved([LAMP, far]))
        sofa = thing(7, "sofa", 0.0, 0.3, 1.0, 0.7, centered=1.0)  # the lamp in front of it covers the center too
        self.assertEqual(resolve.resolve([sofa, center], ""), resolve.Resolved([center, sofa]))
        stale = thing(8, "lamp", 0.4, 0.3, 0.2, 0.3, ago=4)
        self.assertEqual(resolve.resolve([LAMP, stale], "lamp").found, [LAMP, stale])

    def test_the_other_one(self):
        found = resolve.resolve([LAMP, LEFT_LAMP, CHAIR], "lamp", exclude=[7])
        self.assertEqual((ids(found), found.say), ([4], None))
        found = resolve.resolve([LAMP, LEFT_LAMP, CHAIR], "lamp", exclude=[7, 4])
        self.assertEqual((found.found, found.say), ([], "That's the only lamp I've seen."))
        self.assertEqual(resolve.resolve([LAMP], "", exclude=[4]).say, "Nothing else is in view.")

    def test_what_it_looks_like_from_claudes_descriptions(self):
        described = {4: "table lamp Green ceramic lamp with a white shade.", 7: "floor lamp Brass arc floor lamp."}
        for said, picked_id in (("green lamp", 4), ("the floor lamp", 7), ("brass one", 7), ("the green one", 4)):
            found = resolve.resolve([LAMP, LEFT_LAMP], said, described=described)
            self.assertEqual((ids(found)[0], found.say), (picked_id, None), said)
        self.assertIsNotNone(resolve.resolve([LAMP, LEFT_LAMP], "red lamp", described=described).say)  # unknown: ask
        self.assertIsNotNone(resolve.resolve([LAMP, LEFT_LAMP], "green lamp").say)  # nothing to check it against

    def test_nothing_matches(self):
        for objects, said, say in (([LAMP, CHAIR], "mirror", "No mirror in view or seen recently."),
                                   ([LAMP], "the painting", "No painting in view or seen recently."),
                                   ([LAMP], "couch", "No couch in view or seen recently."),
                                   ([], "", "Nothing's in view right now."),
                                   ([], "lamp", "No lamp in view or seen recently.")):
            found = resolve.resolve(objects, said)
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
            with mock.patch.object(seen_module, "BACKEND", f"http://127.0.0.1:{port}"):
                seen = Seen()
                follower = asyncio.create_task(seen.follow(said.append))
                async with asyncio.timeout(5):
                    while len(said) < 2:
                        await asyncio.sleep(0.02)
                follower.cancel()
        self.assertEqual(said, ["Lamp. Finding prices.", SEARCH["spoken_summary"]])
        self.assertEqual((seen.look["capture_id"], len(seen.objects)), ("c_1", 3))


class ActionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.local = FakeLocalParticipant()
        self.room = FakeRoom(self.local, FakeParticipant("agent-x", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT),
                             FakeParticipant("phone-1", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD))
        patcher = mock.patch.object(actions, "get_job_context", lambda: mock.Mock(room=self.room))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_an_action_goes_to_the_phone(self):
        self.assertEqual(await actions.phone_action("add_to_cart", capture_id="c_1"), {"ok": True})
        self.assertEqual(self.local.calls, [("phone-1", "action", {"name": "add_to_cart", "capture_id": "c_1"})])

    async def test_when_the_phone_cant(self):
        self.local.reply = json.dumps({"ok": False, "detail": "The cart is full."})
        with self.assertRaisesRegex(ToolError, "The cart is full."):
            await actions.phone_action("add_to_cart")
        self.local.reply = rtc.RpcError(rtc.RpcError.ErrorCode.UNSUPPORTED_METHOD, "no such method")
        with self.assertRaisesRegex(ToolError, "couldn't do that"):
            await actions.phone_action("undo_last")
        self.room.remote_participants.pop("phone-1")  # only the agent is left
        with self.assertRaisesRegex(ToolError, "isn't connected"):
            await actions.phone_action("start_checkout")


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
        self.assertTrue(claims.video.room.startswith("storefront-"))  # our room, not the one asked for
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
