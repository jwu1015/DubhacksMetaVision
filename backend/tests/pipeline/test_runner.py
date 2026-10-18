"""Tests for runner.py: tracking results -> quick labels and Looks, with Claude and the stores faked."""
import asyncio
import time

import httpx
import pytest
from PIL import Image, ImageFilter

from fakes.stores import Stores
from fakes.tracking import fake_server
from storefront import config
from storefront.live import labels
from storefront.live.scene import WINDOW_S
from storefront.live.stream import Stream
from storefront.live.tracker import TrackedObject, Tracker, TrackResult
from storefront.live.trigger import Dwell
from storefront.models import BBox
from storefront.pipeline import crop, quick_label
from storefront.pipeline.looks import Looks
from storefront.pipeline.quick_label import QuickLabel
from storefront.pipeline.runner import Runner

BOX = BBox(x=0.1, y=0.1, w=0.3, h=0.4)
FRAME = Image.new("RGB", (640, 480), "gray")
T0 = time.time()  # scene memory forgets against the real clock


def result(t: float, *uids: str) -> TrackResult:
    return TrackResult(frame_idx=0, width=640, height=480, latency_ms=10, captured_at=T0 + t,
                       objects=[TrackedObject(uid=u, label="bottle", score=0.9, box=BOX) for u in uids])


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """A runner on a fresh stream: labels after 0.5 s on screen, Looks after 1 s. Returns the runner, the
    messages it published, and the quick label calls."""
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    Stores(monkeypatch)
    live = Stream(Tracker(base_url=""))
    sent, labeled = [], []
    live.publish = sent.append

    async def label(jpeg, category, sam_label):
        labeled.append((jpeg, category, sam_label))
        return QuickLabel(name="green water bottle", price_low=30, price_high=50)
    runner = Runner(live, Looks(live.publish), Dwell(label_ms=500, look_ms=1000), label=label)
    return runner, sent, labeled


def observe(runner: Runner, r: TrackResult):
    runner.live.scene.update(r, FRAME)  # as the stream does, before the runner hears of it
    runner.observe(r, FRAME)


async def settle():
    """Let the work started so far finish (a quick label crops in a worker thread)."""
    for _ in range(5):
        await asyncio.sleep(0.02)


def test_a_quick_label_then_a_look_once_each_timer_is_up(setup):
    runner, sent, labeled = setup

    async def go():
        observe(runner, result(0.0, "1.7"))
        observe(runner, result(0.6, "1.7"))
        await settle()
        assert [m["type"] for m in sent] == ["label"] and runner.looks.get("1.7") is None
        observe(runner, result(1.1, "1.7"))
        observe(runner, result(1.5, "1.7"))
        await settle()

    asyncio.run(go())
    assert sent[0] == {"type": "label", "uid": "1.7", "box": [0.1, 0.1, 0.4, 0.5], "name": "green water bottle",
                       "price_low": 30, "price_high": 50}
    assert len(labeled) == 1 and labeled[0][0][:2] == b"\xff\xd8"  # one call, on a JPEG crop
    assert labeled[0][1:] == (config.DEFAULT_CATEGORY, "bottle")  # told the category and SAM's label
    assert [m["stage"] for m in sent if m["type"] == "look"] == ["started", "described", "results"]


def test_crops_come_from_the_best_frame_not_the_one_that_triggered_them(setup):
    """The camera moves just as the timers run out: the quick label and the Look still crop the sharp frame
    scene memory kept, not the blurred one that triggered them."""
    runner, _, labeled = setup
    sharp = Image.effect_noise((640, 480), 64).convert("RGB")
    blurred = sharp.filter(ImageFilter.GaussianBlur(4))

    async def go():
        for t, img in ((0.0, sharp), (0.6, blurred), (1.1, blurred)):  # the label's timer, then the Look's
            r = result(t, "1.7")
            runner.live.scene.update(r, img)
            runner.observe(r, img)
        await settle()

    asyncio.run(go())
    assert labeled[0][0] == crop.sam_cutout(sharp, BOX, quick_label.EDGE)
    assert runner.looks.get("1.7").cutout == crop.sam_cutout(sharp, BOX)


def test_select_before_the_timer_starts_the_look_now(setup):
    runner, sent, _ = setup

    async def go():
        observe(runner, result(0.0, "1.7"))
        look = runner.select("1.7")
        observe(runner, result(1.5, "1.7"))  # its timer won't start a second one
        await settle()
        return look

    look = asyncio.run(go())
    assert look.selected and look.crop_url
    assert len({m["look_id"] for m in sent if m["type"] == "look"}) == 1
    assert runner.select("9.9") is None  # never tracked


def test_a_forgotten_object_loses_its_look_unless_it_was_picked(setup):
    runner, _, _ = setup

    async def go():
        observe(runner, result(0.0, "1.1", "1.2"))
        observe(runner, result(1.0, "1.1", "1.2"))
        runner.select("1.2")
        observe(runner, result(1.0 + WINDOW_S + 1, "1.3"))  # both forgotten by scene memory
        return set(runner.looks.looks)

    assert asyncio.run(go()) == {"1.2"}


def test_reset_stops_everything_nobody_picked(setup):
    runner, sent, labeled = setup

    async def go():
        observe(runner, result(0.0, "1.1"))
        observe(runner, result(0.6, "1.1"))  # its quick label is under way
        runner.reset()
        await settle()
        observe(runner, result(0.7, "1.1"))  # timers start over
        await settle()

    asyncio.run(go())
    assert [m for m in sent if m["type"] == "label"] == []


def test_end_to_end_frames_to_labels_and_looks(monkeypatch, tmp_path):
    """Frames -> stream -> fake SAM server -> runner -> labels and Looks, with Claude and the stores faked."""
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    Stores(monkeypatch)

    async def prompts(image, category):
        return ["sofa", "lamp", "table"]
    monkeypatch.setattr(labels, "labels_or_fallback", prompts)

    async def label(jpeg, *context):
        return QuickLabel(name="gray sofa", price_low=300, price_high=900)

    async def go():
        server = fake_server(latency=0.02)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server))
        live = Stream(Tracker(base_url="http://fake", client=client))
        runner = Runner(live, Looks(live.publish), Dwell(label_ms=200, look_ms=200), label=label)
        live.on_result, live.on_purge = runner.observe, runner.reset
        messages = live.subscribe()
        await live.start()
        got = []

        async def camera():
            while True:
                live.push_frame(Image.new("RGB", (320, 240), "gray"))
                await asyncio.sleep(1 / 30)
        frames = asyncio.create_task(camera())
        async with asyncio.timeout(10):
            async for m in messages:
                got.append(m)
                done = any(g["type"] == "look" and g["uid"] == "1.1" and g["stage"] == "results" for g in got)
                if done and any(g["type"] == "label" for g in got):
                    break
        frames.cancel()
        await live.stop()
        await messages.aclose()
        await client.aclose()
        return got

    got = asyncio.run(go())
    assert {"label", "look", "objects", "status"} <= {m["type"] for m in got}
    assert next(m for m in got if m["type"] == "label")["name"] == "gray sofa"
    sofa = [m["stage"] for m in got if m["type"] == "look" and m["uid"] == "1.1"]
    assert sofa == ["started", "described", "results"]
