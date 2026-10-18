"""pytest tests/test_prefetch.py  (from backend/). No network: Claude, the stores and the tracker are faked."""
import asyncio
import json
import time

import pytest
from PIL import Image

import main
import prefetch
import rank
import rerank
import search
import stream
import vision
import webrtc
from models import BBox, Item, SearchResponse
from scene import Scene
from tracker import TrackedObject, TrackResult

FRAME = Image.new("RGB", (640, 480), "gray")


def result(*objects: tuple[int, str], frame_idx: int = 1) -> TrackResult:
    return TrackResult(frame_idx=frame_idx, width=640, height=480, latency_ms=10, captured_at=0.0, objects=[
        TrackedObject(id=i, label=label, score=0.9, box=BBox(x=0.1, y=0.1, w=0.3, h=0.4)) for i, label in objects])


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(prefetch, "clock", lambda: now[0])
    monkeypatch.setattr(prefetch, "ENABLED", True)
    monkeypatch.setattr(prefetch, "start_look", None)
    prefetch.reset()
    yield now
    prefetch.reset()


def fake_label(monkeypatch, calls=None):
    async def call_model(jpeg):
        if calls is not None:
            calls.append(jpeg)
        return json.dumps({"name": "black insulated water bottle", "price_low": 30, "price_high": 50})
    monkeypatch.setattr(prefetch, "call_model", call_model)


LABEL = {"name": "black insulated water bottle", "price_low": 30, "price_high": 50}


# ---------- quick labels ----------

def test_quick_label_once_after_half_a_second(clock, monkeypatch):
    calls = []
    fake_label(monkeypatch, calls)

    async def go():
        prefetch.observe(result((1, "bottle")), FRAME)  # just appeared: could be flicker
        assert prefetch.seen[1].labeling is None and prefetch.fields(1) == {}
        clock[0] = 0.6
        prefetch.observe(result((1, "bottle")), FRAME)
        await prefetch.seen[1].labeling
        prefetch.observe(result((1, "bottle")), FRAME)  # labeled already: no second call
        return prefetch.fields(1)

    assert asyncio.run(go()) == LABEL
    assert len(calls) == 1 and calls[0][:2] == b"\xff\xd8"  # a JPEG crop


@pytest.mark.parametrize("reply, expected", [
    ({"name": "  Fuzzy   slippers ", "price_low": 60, "price_high": 25}, ("Fuzzy slippers", 25, 60)),
    ({"name": "slippers", "price_low": 0, "price_high": 30}, None),
    ({"name": " ", "price_low": 10, "price_high": 30}, None),
])
def test_quick_label_cleans_up_the_answer(monkeypatch, reply, expected):
    async def call_model(jpeg):
        return json.dumps(reply)
    monkeypatch.setattr(prefetch, "call_model", call_model)
    q = asyncio.run(prefetch.quick_label(b"jpeg"))
    assert (q.name, q.price_low, q.price_high) == expected if expected else q is None


def test_quick_label_failure_gives_none(monkeypatch):
    async def call_model(jpeg):
        raise RuntimeError("API down")
    monkeypatch.setattr(prefetch, "call_model", call_model)
    assert asyncio.run(prefetch.quick_label(b"jpeg")) is None


def test_reused_id_or_new_session_starts_fresh(clock, monkeypatch):
    fake_label(monkeypatch)

    async def go():
        prefetch.observe(result((1, "bottle"), frame_idx=5), FRAME)
        clock[0] = 0.6
        prefetch.observe(result((1, "bottle"), frame_idx=6), FRAME)
        await prefetch.seen[1].labeling
        assert prefetch.fields(1) == LABEL
        prefetch.observe(result((1, "shoe"), frame_idx=7), FRAME)  # same id, another object
        assert prefetch.fields(1) == {}
        clock[0] = 1.2
        prefetch.observe(result((1, "shoe"), frame_idx=8), FRAME)
        await prefetch.seen[1].labeling
        assert prefetch.fields(1) == LABEL
        prefetch.observe(result((1, "shoe"), frame_idx=0), FRAME)  # a new tracking session
        assert prefetch.fields(1) == {}

    asyncio.run(go())


def test_disabled_does_nothing(clock, monkeypatch):
    monkeypatch.setattr(prefetch, "ENABLED", False)
    clock[0] = 5.0
    prefetch.observe(result((1, "bottle")), FRAME)
    assert prefetch.seen == {}


def test_label_reaches_the_projector_and_the_phone():
    s = stream.Stream(tracker=object())
    s._result = result((3, "bottle"), (4, "shoe"))
    s.scene.update(s._result, FRAME)  # the objects sent out are scene memory's
    s.object_fields = lambda i: LABEL if i == 3 else {}
    event = s._objects_event()
    assert {k: event["objects"][0][k] for k in LABEL} == LABEL  # the projector's objects event
    assert "name" not in event["objects"][1]
    phone = webrtc.detections(event)["objects"]
    assert {k: phone[0][k] for k in ("track_id", *LABEL)} == {"track_id": "t3", **LABEL}
    assert "name" not in phone[1]  # not labeled yet


# ---------- background Looks ----------

def test_background_looks_for_the_three_in_view_longest(clock, monkeypatch):
    fake_label(monkeypatch)
    started = []

    async def start_look(track_id):
        started.append(track_id)
        return type("Detect", (), {"capture_id": f"c_{track_id}"})()

    monkeypatch.setattr(prefetch, "start_look", start_look)
    everyone = [(i, "bottle") for i in range(5)]

    async def go():
        for i, t in enumerate([0.0, 0.1, 0.2, 0.3, 0.4]):  # objects come into view one by one
            clock[0] = t
            prefetch.observe(result(*everyone[:i + 1]), FRAME)
        clock[0] = 1.9
        prefetch.observe(result(*everyone), FRAME)
        await asyncio.sleep(0)
        assert started == []  # nobody in view for 2s yet
        clock[0] = 2.5
        prefetch.observe(result(*everyone), FRAME)
        await asyncio.sleep(0)
        assert started == [0, 1, 2]  # the three in view the longest
        prefetch.observe(result(*everyone), FRAME)
        await asyncio.sleep(0)
        assert started == [0, 1, 2]  # no more while those three are in view
        prefetch.observe(result(*everyone[1:]), FRAME)  # object 0 left the view
        await asyncio.sleep(0)
        assert started == [0, 1, 2, 3]
        await asyncio.sleep(0)
        return prefetch.claim(1), prefetch.claim(4)

    assert asyncio.run(go()) == ("c_1", None)


def test_quiet_background_look_uses_claudes_queries_then_replays_on_tap(monkeypatch):
    events = []
    monkeypatch.setattr(main.stream.live, "publish", events.append)
    monkeypatch.setattr(main.config, "USE_FIXTURES", False)
    claude_item = Item(category="insulated water bottle", description="Green Owala FreeSip, lime spout cover.",
                       search_queries=["Owala FreeSip 24 oz green"])
    searched = []

    async def detect(data, hint=None):
        return claude_item

    async def search_all(cutout_url, item, capture_id=None):
        searched.append(item.search_queries)
        return []

    async def passthrough(cutout_url, products):
        return products

    async def choose(item, products, insight):
        return SearchResponse(capture_id="", best_match=None, dupes=[], used=[], price_insight=insight,
                              spoken_summary="I couldn't find that one for sale.", timing_ms={})

    monkeypatch.setattr(vision, "detect", detect)
    monkeypatch.setattr(search, "search_all", search_all)
    monkeypatch.setattr(rerank, "rerank", passthrough)
    monkeypatch.setattr(rank, "choose", choose)
    # look() takes the object's best frame from scene memory, which forgets a track 15 s after it was seen
    monkeypatch.setattr(main.stream.live, "scene", Scene())
    main.stream.live.scene.update(result((7, "bottle")).model_copy(update={"captured_at": time.time()}),
                                  Image.new("RGB", (640, 480), "green"))

    async def go():
        resp = await main.look(7, quiet=True)  # what prefetch.start_look runs
        cid = resp.capture_id
        assert cid in main.quiet_captures
        await main.search_tasks[cid]
        assert events == []  # nothing announced while nobody has tapped it
        assert searched == [claude_item.search_queries]  # Claude's queries, not the tracker's "bottle"
        monkeypatch.setattr(main.prefetch, "claim", lambda track_id: cid if track_id == 7 else None)
        tapped = await main.look(7)  # the user taps the object
        assert tapped.capture_id == cid and cid not in main.quiet_captures
        return cid

    cid = asyncio.run(go())
    try:
        assert [e["stage"] for e in events] == ["detected", "described", "results"]
        assert all(e["capture_id"] == cid and e["track_id"] == 7 for e in events)
        assert events[1]["item"]["description"] == claude_item.description
    finally:
        for f in main.config.UPLOADS.glob(f"{cid}*"):
            f.unlink()
