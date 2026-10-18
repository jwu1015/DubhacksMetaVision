"""Tests for the voice groundwork (voice/) and POST /voice/token. No LiveKit, no Claude, no network: the main server's stream
events come from a local WebSocket server, its /look route from httpx.MockTransport, and the phone's RPC
from a stand-in room.
"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from livekit import rtc
from livekit.agents import ToolError

from fakes.livekit import FakeLocalParticipant, FakeParticipant, FakeRoom, FakeTextReader
from storefront.voice import actions, resolve
from storefront.voice.seen import Seen


def look(stage, uid=3, selected=True, **extra):
    return {"type": "look", "uid": uid, "look_id": f"l_{uid}", "stage": stage, "label": "lamp", "selected": selected,
            "item": None, "search": None, **extra}


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
OBJECTS = {"type": "objects", "session": 1, "age_ms": 300, "objects": [  # without scene memory's fields
    {"uid": 3, "prompt": "lamp", "score": 0.9, "box": [0.35, 0.1, 0.65, 0.9]},
    {"uid": 4, "prompt": "sofa", "score": 0.8, "box": [0.0, 0.4, 1.0, 1.0]},
    {"uid": 5, "prompt": "vase", "score": 0.7, "box": [0.8, 0.8, 0.9, 0.9]}]}


NOW = 1792286014123  # ms: the scene memory's clock in these tests (resolve.now_ms)


def setUpModule():
    patcher = mock.patch.object(resolve, "now_ms", lambda: NOW)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def thing(uid, prompt, x, y, w, h, ago=None, **fields):
    """A scene memory object: in view, or out of view and last seen ago seconds ago."""
    return {"uid": uid, "prompt": prompt, "score": 0.8, "box": [x, y, x + w, y + h], "in_view": ago is None,
            "last_seen_ts": NOW - (ago or 0) * 1000, "centered": 0.5, "seconds_viewed": 2.0, **fields}


def scene(*objects) -> dict:
    return {"type": "objects", "session": 1, "age_ms": 310, "objects": list(objects)}


PROMPTS = "sofa,chair,table,lamp,rug,bed,shelf,mirror,wall art,vase,pillow,blanket".split(",")


def find(objects, target, **kw) -> resolve.Resolved:
    """resolve(), in a session tracking PROMPTS."""
    return resolve.resolve(objects, target, prompts=PROMPTS, **kw)


def ids(found: resolve.Resolved) -> list:
    return [o["uid"] for o in found.found]


# The scene memory example from the handoff: a lamp on the right in view, a chair passed 5 s ago.
LAMP = thing(4, "lamp", 0.70, 0.30, 0.28, 0.30, centered=0.82, seconds_viewed=3.4)
CHAIR = thing(2, "chair", 0.05, 0.40, 0.30, 0.50, ago=5, centered=0.40, seconds_viewed=1.2)
LEFT_LAMP = thing(7, "lamp", 0.02, 0.30, 0.28, 0.30, centered=0.80)  # as far left of the center as LAMP is right


def picked() -> Seen:
    """A Seen with the lamp picked, described and priced."""
    seen = Seen()
    for m in (OBJECTS, look("started"), look("described", item=ITEM), look("results", item=ITEM, search=SEARCH)):
        seen.update(m)
    return seen


class SeenTest(unittest.TestCase):
    def test_the_scene_and_the_picked_item(self):
        seen = picked()
        self.assertEqual(len(seen.objects), 3)
        self.assertEqual((seen.look["stage"], seen.look["item"], seen.look["search"]), ("results", ITEM, SEARCH))
        seen.update({"type": "status", "tracking": "stopped", "category": "furniture", "prompts": PROMPTS})
        self.assertEqual(seen.objects, [])

    def test_the_sessions_prompts_are_known_kinds_of_object(self):
        seen = picked()
        self.assertEqual(ids(seen.resolve("mirror")), [3, 4, 5])  # not a kind it knows: taken as "that", the lamp
        seen.update({"type": "status", "tracking": "ok", "category": "furniture", "prompts": ["mirror", "lamp"]})
        self.assertEqual(seen.resolve("mirror").say, "No mirror in view or seen recently.")

    def test_every_described_object_is_remembered_but_only_a_pick_is_the_item(self):
        seen = Seen()
        seen.update(look("described", uid=8, selected=False, item={"category": "vase", "description": "Blue glass."}))
        self.assertEqual((seen.described, seen.look), ({8: "vase Blue glass."}, None))

    def test_a_new_tracking_session_forgets_its_uids(self):  # a mode switch or a new category: uids start over
        seen = picked()
        self.assertEqual(seen.described, {3: "floor lamp Tripod floor lamp with a cream drum shade."})
        seen.ranked, seen.tried, seen.asked = [3, 4], [3], []
        seen.update({**scene(), "session": 2})
        self.assertEqual((seen.objects, seen.described, seen.ranked, seen.tried), ([], {}, [], []))
        self.assertIsNone(seen.look["uid"])  # its object is gone from scene memory
        self.assertEqual(seen.look["item"], ITEM)  # but it's still the item picked

    def test_an_empty_view_keeps_what_was_learned(self):  # nothing tracked for a while: same session, same uids
        seen = picked()
        seen.update(scene())
        self.assertEqual(seen.objects, [])
        self.assertEqual((list(seen.described), seen.look["uid"]), ([3], 3))


class ResolveTest(unittest.TestCase):
    def assertShort(self, text):
        self.assertLessEqual(len(text.split()), 12, text)

    def test_the_label_and_what_people_call_it(self):
        objects = [thing(i, label, 0.02 + 0.08 * i, 0.05, 0.05, 0.05) for i, label in enumerate(PROMPTS)]
        for said, label in (("lamp", "lamp"), ("lamps", "lamp"), ("couch", "sofa"), ("the couches", "sofa"),
                            ("armchair", "chair"), ("carpet", "rug"), ("the light", "lamp"), ("painting", "wall art"),
                            ("picture", "wall art"), ("poster", "wall art"), ("wall art", "wall art"),
                            ("bookcase", "shelf"), ("bookshelf", "shelf"), ("cushion", "pillow"), ("throw", "blanket"),
                            ("desk", "table"), ("coffee table", "table"), ("floor lamp", "lamp"),
                            ("throw pillow", "pillow"), ("light blue chair", "chair"),
                            ("lamp next to the couch", "lamp"),
                            ("that mirror over there", "mirror"), ("the bed I just passed", "bed")):
            found = find(objects, said)
            self.assertEqual((found.found[0]["prompt"], found.say), (label, None), said)
        # A tracker label of another kind counts as its kind: a floor lamp is a lamp.
        objects = [thing(1, "floor lamp", 0.1, 0.1, 0.1, 0.1), thing(2, "couch", 0.8, 0.8, 0.1, 0.1)]
        for said, picked_id in (("light", 1), ("lamp", 1), ("floor lamp", 1), ("sofa", 2), ("couch", 2)):
            self.assertEqual(ids(find(objects, said)), [picked_id], said)

    def test_that_is_what_is_at_the_center_of_the_view(self):
        objects = OBJECTS["objects"]  # no in_view, centered or times
        for said in ("", "this", "that one", "what's this thing"):  # the lamp and the sofa cover the center:
            found = find(objects, said)                     # the lamp, in front
            self.assertEqual((ids(found)[0], found.say), (3, None), said)
        self.assertEqual(ids(find(objects[2:], "")), [5])  # nothing covers it: the nearest
        self.assertEqual(ids(find(objects, "sofa")), [4])
        found = find([CHAIR], "")  # only what's in view counts
        self.assertEqual((found.found, found.say), ([], "Nothing's in view right now."))

    def test_an_out_of_view_chair(self):
        for said in ("chair", "what about the chair I just passed", "the one I just passed", "the armchair before"):
            found = find([LAMP, CHAIR], said)
            self.assertEqual((ids(found)[0], found.say), (2, None), said)
        self.assertEqual(ids(find([LAMP, CHAIR], "lamp")), [4])
        # A chair in view, at the edge: "chair" is that one, "the chair I passed" the other.
        edge = thing(8, "chair", 0.0, 0.4, 0.1, 0.2, centered=0.2)
        self.assertEqual(ids(find([edge, LAMP, CHAIR], "chair")), [8, 2])
        self.assertEqual(ids(find([edge, LAMP, CHAIR], "the chair I passed"))[0], 2)

    def test_two_lamps_ask_which(self):
        found = find([LAMP, LEFT_LAMP, CHAIR], "how much is that lamp")
        self.assertEqual((ids(found), found.say), ([4, 7], "The lamp on the left or the right?"))
        for answer, picked_id in (("the left one", 7), ("left", 7), ("the right one", 4)):
            found = find([LAMP, LEFT_LAMP, CHAIR], answer, among=[4, 7])
            self.assertEqual((ids(found)[0], found.say), (picked_id, None), answer)
        self.assertEqual(ids(find([LAMP, LEFT_LAMP, CHAIR], "lamp on the left"))[0], 7)
        self.assertEqual(ids(find([LAMP, LEFT_LAMP, CHAIR], "the lamp right there"))[0], 4)  # not "right"

        high, low = thing(5, "lamp", 0.4, 0.0, 0.2, 0.3), thing(6, "lamp", 0.42, 0.7, 0.2, 0.3)
        self.assertEqual(find([high, low], "lamp").say, "The higher lamp or the lower one?")
        self.assertEqual(ids(find([high, low], "the bottom one", among=[5, 6]))[0], 6)

        passed = thing(9, "lamp", 0.4, 0.3, 0.2, 0.3, ago=1)  # just out of view, against one at the edge in view
        edge = thing(10, "lamp", 0.95, 0.3, 0.05, 0.3, centered=0.0)
        found = find([edge, passed], "lamp")
        self.assertEqual((ids(found), found.say), ([10, 9], "The lamp in front of you, or the one you passed?"))
        self.assertEqual(ids(find([edge, passed], "the one I passed", among=[10, 9]))[0], 9)
        self.assertEqual(ids(find([edge, passed], "the one in front", among=[10, 9]))[0], 10)

        older = thing(11, "lamp", 0.4, 0.3, 0.2, 0.3, ago=2.5)
        found = find([LAMP, passed, older], "lamp", exclude=[4])
        self.assertEqual((ids(found), found.say), ([9, 11], "The lamp you just passed, or the one before?"))
        self.assertEqual(ids(find([LAMP, passed, older], "the one before", among=[9, 11]))[0], 11)
        self.assertEqual(ids(find([LAMP, passed, older], "the lamp I just passed", exclude=[4]))[0], 9)

        vase = thing(12, "vase", 0.58, 0.45, 0.1, 0.1, centered=0.82)  # "that": two things as near the center
        self.assertEqual(find([LAMP, vase, LEFT_LAMP], "").say, "The vase or the lamp?")
        for found in (find([LAMP, LEFT_LAMP], "lamp"), find([LAMP, vase], "")):
            self.assertShort(found.say)

    def test_no_question_when_one_is_clearly_it(self):
        center = thing(5, "lamp", 0.4, 0.2, 0.2, 0.5, centered=0.3)  # covering the center now
        self.assertEqual(find([LAMP, center], "lamp"), resolve.Resolved([center, LAMP]))
        far = thing(6, "lamp", 0.0, 0.0, 0.1, 0.1, centered=0.1)
        self.assertEqual(find([LAMP, far], "lamp"), resolve.Resolved([LAMP, far]))
        sofa = thing(7, "sofa", 0.0, 0.3, 1.0, 0.7, centered=1.0)  # the lamp in front of it covers the center too
        self.assertEqual(find([sofa, center], ""), resolve.Resolved([center, sofa]))
        stale = thing(8, "lamp", 0.4, 0.3, 0.2, 0.3, ago=4)
        self.assertEqual(find([LAMP, stale], "lamp").found, [LAMP, stale])

    def test_the_other_one(self):
        found = find([LAMP, LEFT_LAMP, CHAIR], "lamp", exclude=[7])
        self.assertEqual((ids(found), found.say), ([4], None))
        found = find([LAMP, LEFT_LAMP, CHAIR], "lamp", exclude=[7, 4])
        self.assertEqual((found.found, found.say), ([], "That's the only lamp I've seen."))
        self.assertEqual(find([LAMP], "", exclude=[4]).say, "Nothing else is in view.")

    def test_what_it_looks_like_from_claudes_descriptions(self):
        described = {4: "table lamp Green ceramic lamp with a white shade.", 7: "floor lamp Brass arc floor lamp."}
        for said, picked_id in (("green lamp", 4), ("the floor lamp", 7), ("brass one", 7), ("the green one", 4)):
            found = find([LAMP, LEFT_LAMP], said, described=described)
            self.assertEqual((ids(found)[0], found.say), (picked_id, None), said)
        self.assertIsNotNone(find([LAMP, LEFT_LAMP], "red lamp", described=described).say)  # unknown: ask
        self.assertIsNotNone(find([LAMP, LEFT_LAMP], "green lamp").say)  # nothing to check it against

    def test_nothing_matches(self):
        for objects, said, say in (([LAMP, CHAIR], "mirror", "No mirror in view or seen recently."),
                                   ([LAMP], "the painting", "No painting in view or seen recently."),
                                   ([LAMP], "couch", "No couch in view or seen recently."),
                                   ([], "", "Nothing's in view right now."),
                                   ([], "lamp", "No lamp in view or seen recently.")):
            found = find(objects, said)
            self.assertEqual((found.found, found.say), ([], say), said)
            self.assertShort(say)


class ListenTest(unittest.IsolatedAsyncioTestCase):
    async def test_takes_the_backends_messages_in_the_room(self):
        room, seen = FakeRoom(), Seen()
        seen.listen(room)
        vision, phone = FakeParticipant("vision"), FakeParticipant("phone-1")

        def data(who, body):
            room.emit("data_received", SimpleNamespace(topic="updates", participant=who, data=body))
        data(vision, json.dumps(OBJECTS).encode())
        data(phone, json.dumps(scene()).encode())  # not the backend: ignored
        data(vision, b"not json")                  # skipped
        room.text_handlers["updates"](FakeTextReader(json.dumps(look("results", search=SEARCH))), "vision")
        room.text_handlers["updates"](FakeTextReader(json.dumps(look("results", uid=9))), "phone-1")  # ignored
        await asyncio.sleep(0.01)
        self.assertEqual((len(seen.objects), seen.look["uid"], seen.look["search"]), (3, 3, SEARCH))


class ActionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.local = FakeLocalParticipant()
        self.room = FakeRoom(self.local, FakeParticipant("agent-x", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT),
                             FakeParticipant("phone-1", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD))
        patcher = mock.patch.object(actions, "get_job_context", lambda: mock.Mock(room=self.room))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_an_action_goes_to_the_phone(self):
        self.assertEqual(await actions.phone_action("add_to_cart", look_id="l_1"), {"ok": True})
        self.assertEqual(self.local.calls, [("phone-1", "action", {"name": "add_to_cart", "look_id": "l_1"})])

    async def test_select_asks_the_backend(self):
        self.local.reply = json.dumps({"ok": True, "look": {"uid": "1.4"}})
        self.assertEqual((await actions.select("1.4"))["look"], {"uid": "1.4"})
        self.assertEqual(self.local.calls, [("vision", "select", {"uid": "1.4"})])
        self.local.reply = json.dumps({"ok": False, "detail": "That object isn't tracked any more."})
        with self.assertRaisesRegex(ToolError, "isn't tracked"):
            await actions.select("1.9")

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


if __name__ == "__main__":
    unittest.main()
