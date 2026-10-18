"""Tests for prefetch.py. No network: Claude, the stores and the tracker are faked."""
import asyncio
import base64
import io
import json
import time

import anthropic
import httpx
import pytest
from PIL import Image

from storefront import app, config
from storefront.live import prefetch, stream
from storefront.live.tracker import TrackedObject, TrackResult
from storefront.models import BBox, Item, SearchResponse
from storefront.pipeline import crop, looks, rank, rerank, search, vision
from storefront.rtc import legacy_webrtc as webrtc

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
    async def call_model(system, content, schema, max_tokens, timeout):
        if calls is not None:
            calls.append(base64.b64decode(content[0]["source"]["data"]))
        return json.dumps({"name": "black insulated water bottle", "price_low": 30, "price_high": 50})
    monkeypatch.setattr(prefetch.vlm.haiku, "call", call_model)


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
    async def call_model(system, content, schema, max_tokens, timeout):
        return json.dumps(reply)
    monkeypatch.setattr(prefetch.vlm.haiku, "call", call_model)
    q = asyncio.run(prefetch.quick_label(b"jpeg"))
    assert (q.name, q.price_low, q.price_high) == expected if expected else q is None


def test_quick_label_failure_gives_none(monkeypatch):
    async def call_model(system, content, schema, max_tokens, timeout):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    monkeypatch.setattr(prefetch.vlm.haiku, "call", call_model)
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

def background(item=None, results=None) -> prefetch.Background:
    """A finished background Look: its tasks give item and results. Call it with the loop running."""
    async def value(v):
        return v
    return prefetch.Background(asyncio.create_task(value(item)), asyncio.create_task(value(results)))


def test_background_looks_for_the_three_in_view_longest(clock, monkeypatch):
    fake_label(monkeypatch)
    started = {}

    def start_look(track_id):
        started[track_id] = background()
        return started[track_id]

    monkeypatch.setattr(prefetch, "start_look", start_look)
    everyone = [(i, "bottle") for i in range(5)]

    async def go():
        for i, t in enumerate([0.0, 0.1, 0.2, 0.3, 0.4]):  # objects come into view one by one
            clock[0] = t
            prefetch.observe(result(*everyone[:i + 1]), FRAME)
        clock[0] = 1.9
        prefetch.observe(result(*everyone), FRAME)
        assert list(started) == []  # nobody in view for 2s yet
        clock[0] = 2.5
        prefetch.observe(result(*everyone), FRAME)
        assert list(started) == [0, 1, 2]  # the three in view the longest
        prefetch.observe(result(*everyone), FRAME)
        assert list(started) == [0, 1, 2]  # no more while those three are in view
        prefetch.observe(result(*everyone[1:]), FRAME)  # object 0 left the view
        assert list(started) == [0, 1, 2, 3]
        await asyncio.sleep(0)
        assert prefetch.claim(1) is started[1] and started[1].claimed
        assert prefetch.claim(4) is None  # never looked at

    asyncio.run(go())


def test_forgetting_and_reset_stop_only_the_looks_nobody_picked(clock, monkeypatch):
    fake_label(monkeypatch)
    started = {}

    def start_look(track_id):
        async def forever():
            await asyncio.sleep(3600)
        started[track_id] = prefetch.Background(asyncio.create_task(forever()), asyncio.create_task(forever()))
        return started[track_id]

    monkeypatch.setattr(prefetch, "start_look", start_look)

    async def go():
        prefetch.observe(result((1, "bottle"), (2, "shoe")), FRAME)
        clock[0] = 2.5
        prefetch.observe(result((1, "bottle"), (2, "shoe"), (3, "lamp")), FRAME)
        assert list(started) == [1, 2]
        prefetch.claim(2)  # the user picked the shoe: their Look awaits its tasks now
        clock[0] = 2.6 + prefetch.FORGET_S
        prefetch.observe(result((3, "lamp")), FRAME)  # the bottle and the shoe left the view long ago
        await asyncio.sleep(0)
        assert set(prefetch.seen) == {3} and list(started) == [1, 2, 3]
        assert started[1].describe.cancelled() and started[1].search.cancelled()
        assert not started[2].search.done()
        prefetch.reset()  # a purge: pause, camera switch, disconnect
        await asyncio.sleep(0)
        assert prefetch.seen == {} and started[3].search.cancelled()
        assert not started[2].search.done()  # still the picked Look's

    asyncio.run(go())


def test_a_purge_forgets_everything_prefetch_knows():
    assert app.stream.live.on_purge is prefetch.reset  # app.py's wiring
    s = stream.Stream(tracker=object())
    purged = []
    s.on_purge = lambda: purged.append(True)
    s._purge()  # what stop() (pause, disconnect) and reset() (camera switch) do
    assert purged == [True]


# ---------- background Looks through looks.py ----------

CLAUDE_ITEM = Item(category="insulated water bottle", description="Green Owala FreeSip, lime spout cover.",
                   search_queries=["Owala FreeSip 24 oz green"])


def files() -> set:
    """Everything under uploads/ and logs/."""
    return {p for folder in (config.UPLOADS, config.LOGS) if folder.exists() for p in folder.rglob("*")}


@pytest.fixture
def fakes(monkeypatch):
    """looks.py's Looks on a fresh live stream with Claude and the stores faked; object 7, a bottle, is in
    scene memory. Yields what the fakes were given, the "look" events, and the stores' gate: clear it to
    hold their answer. "fail": the next search fails."""
    calls = {"detect": [], "search": [], "events": [], "fail": False, "gate": asyncio.Event()}
    calls["gate"].set()
    live = stream.Stream(tracker=object())
    live.publish = calls["events"].append
    # scene memory forgets a track 15 s after it was seen
    live.scene.update(result((7, "bottle")).model_copy(update={"captured_at": time.time()}),
                      Image.new("RGB", (640, 480), "green"))

    async def detect(data, hint=None):
        calls["detect"].append(data)
        return CLAUDE_ITEM

    async def search_all(cutout, item, capture_id=None):
        calls["search"].append((cutout, item.search_queries, capture_id))
        await calls["gate"].wait()
        if calls["fail"]:
            calls["fail"] = False
            raise RuntimeError("stores down")
        return []

    async def passthrough(cutout, products):
        return products

    async def choose(item, products, insight):
        return SearchResponse(capture_id="", best_match=None, dupes=[], used=[], price_insight=insight,
                              spoken_summary="I couldn't find that one for sale.", timing_ms={})

    for target, name, value in ((stream, "live", live), (prefetch, "seen", {}),
                                (vision, "detect", detect), (search, "search_all", search_all),
                                (rerank, "rerank", passthrough), (rank, "choose", choose)):
        monkeypatch.setattr(target, name, value)
    yield calls
    for cid in {e["capture_id"] for e in calls["events"]}:
        for path in config.UPLOADS.glob(f"{cid}*"):
            path.unlink()


def seen_with(look: prefetch.Background) -> prefetch.Background:
    prefetch.seen[7] = prefetch.Seen(category="bottle", first=0.0, last=0.0, look=look)
    return look


def test_a_background_look_keeps_only_what_it_found(fakes):
    before, captures = files(), set(looks.captures)

    async def go():
        look = looks.background_look(7)
        return look, await look.search

    look, results = asyncio.run(go())
    assert files() == before and set(looks.captures) == captures  # no capture: no photo, crop or search log
    assert fakes["events"] == []  # nothing announced
    assert look.describe.result() == CLAUDE_ITEM and results.capture_id == "" and results.spoken_summary
    [(cutout, queries, capture_id)] = fakes["search"]
    assert queries == CLAUDE_ITEM.search_queries and capture_id is None  # Claude's queries, not "bottle"
    assert fakes["detect"] == [cutout] and cutout[:2] == b"\xff\xd8"  # Claude and the match score get the crop itself
    track = stream.live.scene.tracks[7]
    padded = crop.pad(track.frame_box, crop.SAM_PAD)
    assert Image.open(io.BytesIO(cutout)).size == crop.crop_image(track.frame, padded).size  # sam_crop's cutout


def test_a_pick_takes_the_finished_background_look_at_once(fakes):
    async def go():
        await seen_with(looks.background_look(7)).search
        resp = await looks.look(7)  # the user taps the object
        return resp, await looks.search_tasks[resp.capture_id]

    resp, found = asyncio.run(go())
    cid, events = resp.capture_id, fakes["events"]
    assert [e["stage"] for e in events] == ["detected", "described", "results"]
    assert all(e["capture_id"] == cid and e["track_id"] == 7 for e in events)
    assert events[1]["item"]["description"] == CLAUDE_ITEM.description
    assert found.capture_id == cid and found.item.description == CLAUDE_ITEM.description
    assert events[2]["search"] == found.model_dump(mode="json")
    assert len(fakes["detect"]) == len(fakes["search"]) == 1  # neither Claude nor the stores asked again
    assert (config.UPLOADS / f"{cid}_cutout.jpg").exists()  # picked: now it's a capture
    assert prefetch.seen[7].look.claimed


def test_a_pick_while_the_background_look_still_searches(fakes):
    fakes["gate"].clear()  # the stores answer when the test says so

    async def go():
        await seen_with(looks.background_look(7)).describe
        resp = await looks.look(7)
        for _ in range(3):
            await asyncio.sleep(0)
        so_far = [e["stage"] for e in fakes["events"]]
        fakes["gate"].set()
        return so_far, await looks.search_tasks[resp.capture_id]

    so_far, found = asyncio.run(go())
    assert so_far == ["detected", "described"]  # Claude's description at once; the results when they come
    assert [e["stage"] for e in fakes["events"]] == ["detected", "described", "results"]
    assert found.capture_id == fakes["events"][0]["capture_id"]
    assert len(fakes["search"]) == 1


def test_after_a_failed_background_search_a_pick_looks_again(fakes):
    fakes["fail"] = True

    async def go():
        look = seen_with(looks.background_look(7))
        with pytest.raises(RuntimeError):
            await look.search
        assert prefetch.claim(7) is None
        resp = await looks.look(7)
        return await looks.search_tasks[resp.capture_id]

    found = asyncio.run(go())
    assert [e["stage"] for e in fakes["events"]] == ["detected", "described", "results"]
    assert found.capture_id == fakes["events"][0]["capture_id"]
    assert len(fakes["detect"]) == len(fakes["search"]) == 2  # a fresh Look: Claude and the stores again
