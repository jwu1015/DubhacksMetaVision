"""Tests for rtc/room.py: the session's LiveKit room, with the room and the server API faked."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from livekit import rtc
from PIL import Image

from fakes.livekit import FakeLiveKitAPI, FakeParticipant, FakeRoom
from fakes.stores import Stores
from storefront import config
from storefront.live.stream import Stream
from storefront.live.tracker import TrackedObject, Tracker, TrackResult
from storefront.live.trigger import Dwell
from storefront.models import BBox
from storefront.pipeline.looks import Looks
from storefront.pipeline.runner import Runner
from storefront.rtc import room as room_module
from storefront.rtc.room import Rooms, to_image

PHONE = FakeParticipant("phone-1")


def rgba(width: int, height: int, left=(255, 0, 0)) -> rtc.VideoFrame:
    """A frame whose left column is `left`, the rest black."""
    data = bytearray(width * height * 4)
    for y in range(height):
        data[y * width * 4:y * width * 4 + 4] = bytes([*left, 255])
    return rtc.VideoFrame(width, height, rtc.VideoBufferType.RGBA, data)


def event(frame: rtc.VideoFrame, rotation=rtc.VideoRotation.VIDEO_ROTATION_0) -> rtc.VideoFrameEvent:
    return rtc.VideoFrameEvent(frame=frame, timestamp_us=0, rotation=rotation)


def test_frames_come_out_upright():
    img = to_image(event(rgba(4, 2)))
    assert (img.size, img.mode, img.getpixel((0, 0)), img.getpixel((3, 0))) == ((4, 2), "RGB", (255, 0, 0), (0, 0, 0))
    turned = to_image(event(rgba(4, 2), rtc.VideoRotation.VIDEO_ROTATION_90))  # a quarter turn clockwise
    assert turned.size == (2, 4) and turned.getpixel((0, 0)) == turned.getpixel((1, 0)) == (255, 0, 0)
    i420 = rgba(4, 2).convert(rtc.VideoBufferType.I420)  # other formats are converted
    assert to_image(event(i420)).size == (4, 2)


@pytest.fixture
def session(monkeypatch, tmp_path):
    """An open session: (rooms, the fake room, the fake server API, the stream)."""
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    monkeypatch.setattr(room_module, "PHONE_GRACE_S", 0.05)
    Stores(monkeypatch)
    live = Stream(Tracker(base_url=""))  # no segmentation server: tracking fails quietly
    runner = Runner(live, Looks(live.publish), Dwell(label_ms=10_000, look_ms=10_000))
    room, lk = FakeRoom(), FakeLiveKitAPI()

    async def join(name):
        return room
    rooms = Rooms(live, runner, join=join, livekit=lk)
    return rooms, room, lk, live


async def settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


def test_open_makes_a_room_for_the_category_and_a_new_one_leaves_the_old(session):
    rooms, room, lk, live = session

    async def go():
        first = await rooms.open("kitchen utensils")
        assert live.tracker.category == "kitchen utensils"
        assert lk.calls == [("create_room", first, room_module.EMPTY_TIMEOUT_S)]
        assert set(room.local_participant.rpc) == {"select", "mode", "pause", "category", "voice"}
        second = await rooms.open()
        assert second != first and rooms.current.name == second and room.disconnected
        assert live.tracker.category == config.DEFAULT_CATEGORY
        await rooms.close()
        assert rooms.current is None

    asyncio.run(go())


def test_messages_go_out_on_the_updates_topic_and_a_joiner_gets_the_state(session):
    rooms, room, _, live = session

    async def go():
        await rooms.open()
        await settle()
        lp = room.local_participant
        lp.sent.clear()
        live.publish({"type": "label", "uid": "1.4", "name": "brass lamp"})
        live.publish({"type": "look", "uid": "1.4", "stage": "started"})
        await settle()
        assert [(kind, m["type"], to, topic) for kind, m, to, topic in lp.sent] == [
            ("data", "label", None, "updates"), ("text", "look", None, "updates")]
        lp.sent.clear()
        room.join(FakeParticipant("viewer-1"))
        await settle()
        assert [(m["type"], to) for _, m, to, _ in lp.sent] == [("status", ["viewer-1"]), ("label", ["viewer-1"]),
                                                               ("look", ["viewer-1"])]
        await rooms.close()

    asyncio.run(go())


def test_select_picks_a_tracked_object(session):
    rooms, room, _, live = session

    async def go():
        await rooms.open()
        r = TrackResult(frame_idx=0, width=640, height=480, latency_ms=1, captured_at=time.time(), objects=[
            TrackedObject(uid="1.4", label="lamp", score=0.9, box=BBox(x=0.1, y=0.1, w=0.3, h=0.4))])
        live.scene.update(r, Image.new("RGB", (640, 480), "gray"))
        lp = room.local_participant
        picked = await lp.call("select", {"uid": "1.4"})
        assert picked["ok"] and picked["look"]["uid"] == "1.4" and picked["look"]["selected"]
        assert (await lp.call("select", {"uid": "9.9"}))["ok"] is False
        assert (await lp.call("select", "not json"))["detail"] == "the payload must be a JSON object"
        await rooms.close()

    asyncio.run(go())


def test_mode_pause_and_category(session):
    rooms, room, _, live = session

    async def go():
        await rooms.open()
        lp = room.local_participant
        assert (await lp.call("mode", {"mode": "tv"}))["ok"] is False
        assert (await lp.call("mode", {"mode": "glasses"}))["ok"]
        assert (await lp.call("pause", {"paused": "yes"}))["ok"] is False
        await live.start()
        assert (await lp.call("pause", {"paused": True}))["ok"] and not live.running
        assert (await lp.call("pause", {"paused": False}))["ok"] and live.running
        assert (await lp.call("category", {"category": "  "}))["ok"] is False
        assert (await lp.call("category", {"category": " small   electronics "}))["ok"]
        assert live.tracker.category == "small electronics"
        await rooms.close()

    asyncio.run(go())


def test_voice_brings_the_agent_and_sends_it_away(session):
    rooms, room, lk, _ = session

    async def go():
        name = await rooms.open()
        lp = room.local_participant
        assert (await lp.call("voice", {"on": True}))["ok"]
        assert (await lp.call("voice", {"on": True}))["ok"]  # already there
        room.join(FakeParticipant("agent-1", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT))
        assert (await lp.call("voice", {"on": False}))["ok"]
        assert lk.calls[1:] == [("create_dispatch", config.VOICE_AGENT, name), ("delete_dispatch", "d1", name),
                                ("remove_participant", name, "agent-1")]
        await rooms.close()

    asyncio.run(go())


def test_a_voice_room_tells_the_agent_whats_tracked_and_leaves_tracking_alone(session, monkeypatch):
    rooms, room, lk, live = session
    videos = []
    monkeypatch.setattr(room_module.rtc, "VideoStream", lambda track, format=None: videos.append(track))

    async def go():
        await live.start()  # the current app's camera feeds the stream over legacy WebRTC
        name = await rooms.open_voice()
        assert name.startswith("storefront-voice-") and rooms.voice_room.name == name and rooms.current is None
        assert lk.calls == [("create_room", name, room_module.EMPTY_TIMEOUT_S),
                            ("create_dispatch", config.VOICE_AGENT, name)]  # once the backend is in
        live.publish({"type": "label", "uid": "1.4", "name": "brass lamp"})
        await settle()
        lp = room.local_participant
        lp.sent.clear()
        room.join(FakeParticipant("agent-1", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT))
        await settle()
        assert [(m["type"], to) for _, m, to, _ in lp.sent] == [("status", ["agent-1"]), ("label", ["agent-1"])]
        room.join(PHONE)
        room.emit("track_subscribed", SimpleNamespace(kind=rtc.TrackKind.KIND_VIDEO), None, PHONE)
        room.leave(PHONE.identity)
        await asyncio.sleep(0.1)  # past the grace period
        assert live.running and videos == []  # its video isn't this room's: the legacy path tracks it
        await rooms.close()
        assert room.disconnected and live.running
        await live.stop()

    asyncio.run(go())


def test_the_voice_room_and_the_sessions_room_dont_replace_each_other(session):
    rooms, _, _, _ = session
    made = {}

    async def join(name):
        made[name] = FakeRoom()
        return made[name]
    rooms.join = join

    async def go():
        voice = await rooms.open_voice()
        current = await rooms.open()
        assert (rooms.voice_room.name, rooms.current.name, made[voice].disconnected) == (voice, current, False)
        again = await rooms.open_voice()
        assert made[voice].disconnected and not made[current].disconnected and rooms.voice_room.name == again
        await rooms.close()
        assert made[again].disconnected and made[current].disconnected

    asyncio.run(go())


def test_the_phones_video_feeds_the_stream_and_its_leaving_stops_tracking(session, monkeypatch):
    rooms, room, _, live = session

    class Video:
        def __init__(self, track, format=None):
            pass

        def __aiter__(self):
            return self.frames()

        async def frames(self):
            yield event(rgba(32, 16), rtc.VideoRotation.VIDEO_ROTATION_90)
            await asyncio.sleep(10)  # the track stays open

        async def aclose(self):
            pass
    monkeypatch.setattr(room_module.rtc, "VideoStream", Video)

    async def go():
        await rooms.open()
        room.join(PHONE)
        room.emit("track_subscribed", SimpleNamespace(kind=rtc.TrackKind.KIND_VIDEO), None, PHONE)
        await settle()
        assert live.running and live.latest_frame().size == (16, 32)  # upright
        room.leave(PHONE.identity)
        room.join(PHONE)  # back within the grace period
        await asyncio.sleep(0.1)
        assert live.running
        room.leave(PHONE.identity)
        await asyncio.sleep(0.1)
        assert not live.running
        await rooms.close()

    asyncio.run(go())
