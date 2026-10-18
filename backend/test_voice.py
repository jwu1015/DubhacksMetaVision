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

    def test_center_object(self):
        seen = voice.Seen()
        self.assertIsNone(seen.center_object())
        seen.update(OBJECTS)
        self.assertEqual(seen.center_object()["id"], 3)  # the lamp and the sofa cover the center; the lamp is smaller
        seen.update({**OBJECTS, "objects": OBJECTS["objects"][2:]})
        self.assertEqual(seen.center_object()["id"], 5)  # nothing covers it: the nearest

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

    async def test_look_at_this_looks_at_the_center_object(self):
        seen = voice.Seen()
        with self.assertRaisesRegex(ToolError, "isn't streaming"):
            await self.agent(seen).look_at_this(None)
        seen.update(OBJECTS)
        self.assertIsNone(await self.agent(seen).look_at_this(None))  # the app says what it found
        self.assertEqual(self.posts, ["/look/3"])
        self.look_status = 404
        with self.assertRaisesRegex(ToolError, "Lost sight"):
            await self.agent(seen).look_at_this(None)
        self.look_status = 500
        with self.assertRaisesRegex(ToolError, "couldn't look"):
            await self.agent(seen).look_at_this(None)

    async def test_look_at_this_on_the_item_just_picked_starts_nothing(self):
        result = await self.agent(picked()).look_at_this(None)
        self.assertIn("already the current item", result)
        self.assertEqual(self.posts, [])

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
