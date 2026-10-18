"""Tests for looks.py, with Claude and the stores faked."""
import asyncio
import json
import time

import pytest

from fakes.stores import CLAUDE_ITEM, Stores
from storefront import config
from storefront.models import BBox, PriceInsight, Product, SearchResponse
from storefront.pipeline.looks import FAILED, Look, Looks

BOX = BBox(x=0.1, y=0.2, w=0.3, h=0.4)
JPEG = b"\xff\xd8 a crop"


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "UPLOADS", tmp_path)
    return Stores(monkeypatch)


def run(coro):
    return asyncio.run(coro)


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def test_a_look_describes_then_searches_on_claudes_queries(stores, tmp_path):
    sent = []

    async def go():
        looks = Looks(sent.append)
        looks.start("1.7", "bottle", BOX, JPEG)
        looks.start("1.7", "bottle", BOX, JPEG)  # it has one already
        await settle()

    run(go())
    assert [m["stage"] for m in sent] == ["started", "described", "results"]
    assert {m["uid"] for m in sent} == {"1.7"} and len({m["look_id"] for m in sent}) == 1
    assert sent[0]["box"] == [0.1, 0.2, 0.4, 0.6] and sent[0]["label"] == "bottle"
    assert sent[1]["item"]["category"] == CLAUDE_ITEM.category
    assert sent[2]["search"]["capture_id"] == sent[2]["look_id"] and sent[2]["search"]["item"] == sent[1]["item"]
    assert stores.described == [JPEG] and stores.contexts == [(None, "bottle")]
    assert stores.searched == [(JPEG, CLAUDE_ITEM.search_queries, sent[0]["look_id"])]  # the crop itself, in memory
    assert list(tmp_path.iterdir()) == [] and not sent[2]["selected"]  # nobody picked it: nothing written


def test_claude_is_told_the_category(stores):
    async def go():
        Looks(lambda m: None).start("1.7", "phone", BOX, JPEG, category="electronics")
        await settle()

    run(go())
    assert stores.contexts == [("electronics", "phone")]


def test_claude_failing_searches_the_label(stores):
    sent = []
    stores.item = RuntimeError("Claude down")

    async def go():
        Looks(sent.append).start("1.7", "bottle", BOX, JPEG)
        await settle()

    run(go())
    assert [m["stage"] for m in sent] == ["started", "results"]
    assert stores.searched[0][1] == ["bottle"]


def test_a_failed_search_says_so(stores):
    sent = []
    stores.fail = True

    async def go():
        Looks(sent.append).start("1.7", "bottle", BOX, JPEG)
        await settle()

    run(go())
    assert (sent[-1]["stage"], sent[-1]["message"]) == ("failed", FAILED)


def test_picking_writes_the_crop_and_searches_by_its_url(stores, tmp_path):
    sent = []
    stores.gate.clear()

    async def go():
        looks = Looks(sent.append)
        looks.start("1.7", "bottle", BOX, JPEG)
        await settle()  # described; the search waits on the gate
        look = looks.select("1.7")
        assert looks.select("1.7") is look  # picked already
        stores.gate.set()
        await settle()
        return look

    look = run(go())
    path = tmp_path / f"{look.look_id}.jpg"
    assert path.read_bytes() == JPEG
    assert look.crop_url == f"{config.PUBLIC_BASE_URL}/uploads/{path.name}"
    assert [(m["stage"], m["selected"]) for m in sent] == [("started", False), ("described", False),
                                                          ("described", True), ("results", True)]
    assert stores.searched[0][0] == JPEG  # its search had started before the pick


def test_at_most_max_looks_run_and_the_most_recent_waiting_goes_next(stores):
    sent = []
    stores.gate.clear()

    async def go():
        looks = Looks(sent.append, max_running=2)
        for uid in ("a", "b", "c", "d"):
            looks.start(uid, "thing", BOX, JPEG)
        await settle()
        assert [s[2] for s in stores.searched] == [looks.get("a").look_id, looks.get("b").look_id]
        stores.gate.set()
        await settle()
        return [looks.get(u).look_id for u in "abcd"]

    ids = run(go())
    order = [s[2] for s in stores.searched]
    assert order == [ids[0], ids[1], ids[3], ids[2]]  # d, started last, before c


def test_picking_a_waiting_look_runs_it_now_and_searches_by_the_crops_url(stores):
    stores.gate.clear()

    async def go():
        looks = Looks(lambda m: None, max_running=1)
        looks.start("a", "thing", BOX, JPEG)
        looks.start("b", "thing", BOX, JPEG)
        await settle()
        looks.select("b")
        await settle()
        return looks.get("b")

    b = run(go())
    assert (b.crop_url, b.look_id) in [(cutout, look_id) for cutout, _, look_id in stores.searched]  # Lens can fetch it


def test_forget_and_reset_stop_only_the_looks_nobody_picked(stores):
    stores.gate.clear()

    async def go():
        looks = Looks(lambda m: None)
        for uid in ("a", "b", "c"):
            looks.start(uid, "thing", BOX, JPEG)
        await settle()
        tasks = {u: looks.get(u).task for u in "abc"}
        looks.select("b")
        looks.forget("a")
        looks.forget("b")
        await settle()
        assert tasks["a"].cancelled() and not tasks["b"].done()
        looks.reset()
        await settle()
        assert tasks["c"].cancelled() and not tasks["b"].done()
        return set(looks.looks)

    assert run(go()) == {"b"}


def test_the_users_taps_are_remembered_the_latest_first_and_the_agents_picks_arent(stores):
    async def go():
        looks = Looks(lambda m: None)
        for uid in "abcd":
            looks.start(uid, "thing", BOX, JPEG)
        await settle()
        looks.select("a")
        time.sleep(0.002)
        looks.select("c", by="voice")  # the voice agent's pick, to run its Look sooner: not a tap
        looks.select("b")
        assert [lk.uid for lk in looks.picked()] == ["b", "a"]
        assert (looks.get("c").selected, looks.get("c").picked_at) == (True, None)
        looks.forget("c")  # its object left scene memory: dropped like a Look nobody picked
        assert looks.get("c") is None
        looks.select("d", by="voice")
        time.sleep(0.002)
        looks.select("d")  # then the user taps it: now it's a tap, the latest
        assert [lk.uid for lk in looks.picked()] == ["d", "b", "a"]
        looks.reset()
        return set(looks.looks)

    assert run(go()) == {"a", "b", "d"}


def test_a_looks_summary_is_small():
    def product(title, price, tier="new"):
        return Product(title=title, store="Store", price=price, url="https://shop.example/" + "x" * 300,
                       image_url="https://img.example/" + "y" * 300, source="shopping", tier=tier, source_rank=0)
    search = SearchResponse(capture_id="l_1", best_match=product("Owala FreeSip " + "x" * 100, 27.99),
                            dupes=[product("Look-alike bottle", 14.5, "dupe")], used=[product("Used Owala", 12.0, "used")],
                            price_insight=PriceInsight(typical_low=10, typical_high=30, median=20, sample_size=5,
                                                       best_match_verdict="fair", verdict_text="Fair."),
                            spoken_summary="Store has it for about 28 dollars.", timing_ms={})
    s = Look("1.7", "bottle", BOX, JPEG, item=CLAUDE_ITEM, stage="results", search=search).summary()
    assert (s["name"], s["stage"], len(s["best_match"]["title"])) == (CLAUDE_ITEM.category, "results", 80)
    assert (s["cheapest"]["price"], s["cheapest"]["tier"]) == (12.0, "used")
    assert "url" not in s["best_match"] and len(json.dumps(s)) < 800  # a dozen fit in one RPC answer
    assert Look("1.8", "mug", BOX, JPEG).summary()["best_match"] is None  # no results yet
