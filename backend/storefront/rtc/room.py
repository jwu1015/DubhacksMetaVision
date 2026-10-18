"""The session's LiveKit room: the phone, the backend ("vision"), any projector pages and, on demand, the
voice agent. POST /session/token opens a new room (Rooms.open): the backend joins it and leaves the old one.

    phone -> vision   video track: the glasses' frames, into stream.live
    vision -> all     data packets, topic "updates": the "objects", "label" and "status" messages (models.py)
                      text streams, topic "updates": the "look" messages (results can pass a data packet's ~15 KB)
    phone -> vision   RPCs, each answering {"ok": true, ...} or {"ok": false, "detail"}:
                        select {uid, by?}        pick an object; answers its Look ("look"). by "voice": the
                                                 voice agent picked it, to run its Look sooner: not a tap
                        mode {mode}              "glasses" | "phone": a camera switch, so forget what was tracked
                        pause {paused}           stop tracking and purge it / start again
                        category {category}      track things in another category
                        voice {on}               bring the voice agent in / send it away
                        memory {look_id?}        for the voice agent: what the user tapped, the latest first
                                                 ("picked": Look summaries, across tracking restarts), or one
                                                 Look in full ("look")

Someone joining gets the current state, sent to them alone. When the phone leaves for PHONE_GRACE_S,
tracking stops.

POST /voice/token opens a voice room instead (Rooms.open_voice), for the current app: its camera reaches the
stream over the legacy WebRTC path, so the backend joins the room only to pass the same messages to the voice
agent, and leaves tracking alone.
"""
import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from livekit import api, rtc
from PIL import Image

from storefront import config
from storefront.live.stream import Stream
from storefront.pipeline.runner import Runner
from storefront.rtc import tokens

log = logging.getLogger("storefront.room")

PHONE_GRACE_S = 10.0  # a phone that drops out this long has left: tracking stops
EMPTY_TIMEOUT_S = 120  # LiveKit closes a room this long after its last participant left
MAX_CATEGORY = 100     # characters
MEMORY_ITEMS = 12      # the latest taps the memory RPC lists
MEMORY_BYTES = 12_000  # and at most this much of them: an RPC answer can't pass 15 KiB
DEGREES = {rtc.VideoRotation.VIDEO_ROTATION_90: 90, rtc.VideoRotation.VIDEO_ROTATION_180: 180,
           rtc.VideoRotation.VIDEO_ROTATION_270: 270}


def to_image(event: rtc.VideoFrameEvent) -> Image.Image:
    """A received frame as an upright RGB image (the rotation is clockwise; PIL's is counterclockwise)."""
    frame = event.frame
    if frame.type != rtc.VideoBufferType.RGBA:  # VideoStream(format=RGBA) delivers RGBA already
        frame = frame.convert(rtc.VideoBufferType.RGBA)
    img = Image.frombuffer("RGBA", (frame.width, frame.height), bytes(frame.data), "raw", "RGBA", 0, 1).convert("RGB")
    degrees = DEGREES.get(event.rotation, 0)
    return img.rotate(-degrees, expand=True) if degrees else img


def answer(ok: bool = True, detail: str | None = None, **fields) -> str:
    return json.dumps({"ok": ok, **({"detail": detail} if detail else {}), **fields})


def is_phone(p: rtc.RemoteParticipant) -> bool:
    return p.identity.startswith(tokens.PHONE)


class Session:
    """One room, joined as "vision". tracking: the phone's camera is in this room, so its video goes into the
    stream and tracking stops when the phone leaves. False for a voice room, whose phone's camera reaches the
    stream another way."""

    def __init__(self, name: str, room: rtc.Room, live: Stream, runner: Runner,
                 livekit: Callable[[], api.LiveKitAPI], tracking: bool = True):
        self.name, self.room, self.live, self.runner, self.livekit = name, room, live, runner, livekit
        self.tracking = tracking
        self.paused = False
        self._dispatch_id: str | None = None  # the voice agent's, while it's wanted
        self._grace: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        room.on("participant_connected", self._on_join)
        room.on("participant_disconnected", self._on_leave)
        room.on("track_subscribed", self._on_track)
        for method, handler in (("select", self._select), ("mode", self._mode), ("pause", self._pause),
                                ("category", self._category), ("voice", self._voice), ("memory", self._memory)):
            room.local_participant.register_rpc_method(method, self._rpc(handler))
        self._spawn(self._publish())

    def _spawn(self, coro: Awaitable) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---------- messages out ----------

    async def _publish(self):
        async for m in self.live.subscribe():
            await self._send(m)

    async def _send(self, m: dict, to: list[str] | None = None):
        """One message, to everyone or to `to`. A failed send is logged and dropped: the stream goes on."""
        text, lp = json.dumps(m), self.room.local_participant
        try:
            if m["type"] == "look":
                await lp.send_text(text, topic=tokens.UPDATES, destination_identities=to)
            else:
                await lp.publish_data(text, reliable=True, topic=tokens.UPDATES, destination_identities=to or [])
        except Exception as e:
            log.warning("room %s: sending a %s message failed (%r)", self.name, m["type"], e)

    async def _catch_up(self, identity: str):
        for m in self.live.snapshot():
            await self._send(m, [identity])

    # ---------- participants and video ----------

    def _on_join(self, p: rtc.RemoteParticipant):
        log.info("room %s: %s joined", self.name, p.identity)
        if is_phone(p) and self._grace:
            self._grace.cancel()
            self._grace = None
        self._spawn(self._catch_up(p.identity))

    def _on_leave(self, p: rtc.RemoteParticipant):
        log.info("room %s: %s left", self.name, p.identity)
        if self.tracking and is_phone(p) and not any(is_phone(o) for o in self.room.remote_participants.values()):
            self._grace = self._spawn(self._phone_gone())

    async def _phone_gone(self):
        await asyncio.sleep(PHONE_GRACE_S)
        log.info("room %s: the phone left; tracking stops", self.name)
        await self.live.stop()

    def _on_track(self, track: rtc.Track, publication: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant):
        if self.tracking and track.kind == rtc.TrackKind.KIND_VIDEO and is_phone(p):
            log.info("room %s: video from %s", self.name, p.identity)
            self._spawn(self._receive(track))

    async def _receive(self, track: rtc.Track):
        video = rtc.VideoStream(track, format=rtc.VideoBufferType.RGBA)
        try:
            async for event in video:
                if self.paused:  # nothing is kept while the camera is paused
                    continue
                if not self.live.running:
                    await self.live.start()
                self.live.push_frame(to_image(event))
        finally:
            await video.aclose()

    # ---------- RPCs ----------

    def _rpc(self, handler: Callable[[dict], Awaitable[str]]):
        async def call(data: rtc.RpcInvocationData) -> str:
            try:
                payload = json.loads(data.payload or "{}")
            except ValueError:
                payload = None
            if not isinstance(payload, dict):
                return answer(False, "the payload must be a JSON object")
            log.info("room %s: %s %s %s", self.name, data.caller_identity, data.method, data.payload[:80])
            return await handler(payload)
        return call

    async def _select(self, payload: dict) -> str:
        uid = payload.get("uid")
        by = "voice" if payload.get("by") == "voice" else "user"
        look = self.runner.select(uid, by) if isinstance(uid, str) else None
        if look is None:
            return answer(False, "That object isn't tracked any more.")
        return answer(look=look.message_dict())

    async def _memory(self, payload: dict) -> str:
        """What the voice agent recalls: the Looks the user tapped, the latest first, or one Look in full."""
        looks = self.runner.looks
        if look_id := payload.get("look_id"):
            look = next((lk for lk in looks.looks.values() if lk.look_id == look_id), None)
            return answer(look=look.message_dict()) if look else answer(False, "That item isn't remembered.")
        picked: list[dict] = []
        for look in looks.picked()[:MEMORY_ITEMS]:
            picked.append(look.summary())
            if len(json.dumps(picked)) > MEMORY_BYTES:
                picked.pop()
                break
        return answer(picked=picked)

    async def _mode(self, payload: dict) -> str:
        if payload.get("mode") not in ("glasses", "phone"):
            return answer(False, 'mode must be "glasses" or "phone"')
        if self.live.running:  # the phone swapped cameras: what was tracked belongs to the old one
            await self.live.reset()
        return answer()

    async def _pause(self, payload: dict) -> str:
        paused = payload.get("paused")
        if not isinstance(paused, bool):
            return answer(False, "paused must be true or false")
        if paused != self.paused:
            self.paused = paused
            await (self.live.stop() if paused else self.live.start())
        return answer()

    async def _category(self, payload: dict) -> str:
        category = " ".join(str(payload.get("category") or "").split())[:MAX_CATEGORY]
        if not category:
            return answer(False, "category must be a few words")
        await self.live.set_category(category)
        return answer()

    async def _voice(self, payload: dict) -> str:
        on = payload.get("on")
        if not isinstance(on, bool):
            return answer(False, "on must be true or false")
        if not await self.voice(on):
            return answer(False, "The voice assistant isn't available.")
        return answer()

    async def voice(self, on: bool) -> bool:
        """Bring the voice agent in, or send it away. False if that failed."""
        try:
            async with self.livekit() as lk:
                if on and not self._dispatch_id:
                    dispatch = await lk.agent_dispatch.create_dispatch(
                        api.CreateAgentDispatchRequest(agent_name=config.VOICE_AGENT, room=self.name))
                    self._dispatch_id = dispatch.id
                elif not on and self._dispatch_id:
                    await self._dismiss(lk)
        except Exception as e:
            log.warning("room %s: voice %s failed (%r)", self.name, on, e)
            return False
        return True

    async def _dismiss(self, lk: api.LiveKitAPI):
        """Send the voice agent away: no new job, and the one in the room leaves."""
        await lk.agent_dispatch.delete_dispatch(self._dispatch_id, self.name)
        self._dispatch_id = None
        for p in list(self.room.remote_participants.values()):
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                await lk.room.remove_participant(api.RoomParticipantIdentity(room=self.name, identity=p.identity))

    # ---------- the end ----------

    async def close(self):
        """Leave the room and stop tracking (a voice room leaves tracking alone). Never raises."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self.room.disconnect()
        except Exception as e:
            log.warning("room %s: leaving failed (%r)", self.name, e)
        if self.tracking:
            await self.live.stop()
        log.info("room %s: left", self.name)


async def join(name: str) -> rtc.Room:
    room = rtc.Room()
    await room.connect(config.LIVEKIT_URL, tokens.vision(name))
    return room


def livekit_api() -> api.LiveKitAPI:
    return api.LiveKitAPI(config.LIVEKIT_URL, config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)


class Rooms:
    """One session at a time, and one voice room: opening a new one leaves the old one."""

    def __init__(self, live: Stream, runner: Runner, join: Callable[[str], Awaitable[rtc.Room]] = join,
                 livekit: Callable[[], api.LiveKitAPI] = livekit_api):
        self.live, self.runner, self.join, self.livekit = live, runner, join, livekit
        self.current: Session | None = None
        self.voice_room: Session | None = None
        self._lock = asyncio.Lock()

    async def open(self, category: str | None = None) -> str:
        """A new room with the backend in it, tracking things in `category`; its name."""
        async with self._lock:
            await self._close()
            name = "storefront-" + uuid.uuid4().hex[:8]
            self.current = await self._session(name, tracking=True)
            self.live.tracker.category = category or config.DEFAULT_CATEGORY
            log.info("room %s: open, tracking %r", name, self.live.tracker.category)
            return name

    async def open_voice(self) -> str:
        """A new voice room for the current app (POST /voice/token), with the backend and the voice agent in it;
        its name. The agent is sent for once the backend is in, so the first messages it gets are the state."""
        async with self._lock:
            await self._close_voice()
            name = "storefront-voice-" + uuid.uuid4().hex[:8]
            self.voice_room = await self._session(name, tracking=False)
            await self.voice_room.voice(True)
            log.info("room %s: open for the voice assistant", name)
            return name

    async def close(self):
        async with self._lock:
            await self._close()
            await self._close_voice()

    async def _session(self, name: str, tracking: bool) -> Session:
        async with self.livekit() as lk:
            await lk.room.create_room(api.CreateRoomRequest(name=name, empty_timeout=EMPTY_TIMEOUT_S))
        return Session(name, await self.join(name), self.live, self.runner, self.livekit, tracking)

    async def _close(self):
        session, self.current = self.current, None
        if session:
            await session.close()

    async def _close_voice(self):
        session, self.voice_room = self.voice_room, None
        if session:
            await session.close()
