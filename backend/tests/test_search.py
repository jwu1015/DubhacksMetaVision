"""pytest tests/test_search.py  (from backend/). No network: SerpAPI calls are faked."""
import asyncio
import json
from pathlib import Path

import pytest

import search
from models import Item, Product
from search import (dedupe, normalize_url, parse_ebay, parse_lens, parse_price, parse_shopping,
                    queries_from_lens)

DATA = Path(__file__).resolve().parent / "data"


def raw(name: str) -> dict:
    return json.loads((DATA / name).read_text())


def product(url="https://shop.example/p/1", store="Target", title="Tripod Floor Lamp", source="shopping", rank=0):
    return Product(title=title, store=store, price=50.0, url=url, source=source, source_rank=rank)


# ---------- parse_price ----------

def test_price_string_with_comma():
    assert parse_price(None, "$1,299.00") == 1299.0


def test_price_range_takes_low_end():
    assert parse_price(None, "$80 – $120") == 80.0


def test_price_prefers_numeric_field():
    assert parse_price(1299.0, "$1,300.00") == 1299.0


@pytest.mark.parametrize("extracted, text", [(None, "Free"), (0, "Free"), (None, None), (None, "")])
def test_price_free_or_missing_is_none(extracted, text):
    assert parse_price(extracted, text) is None


@pytest.mark.parametrize("extracted, text, currency", [
    (55, "€55", None),
    (170280, "¥170,280*", "¥"),  # the shape of SerpAPI's Lens docs example
    (None, "CA$80.00", None),
    (20, "£20.00", None),
    (99, None, "EUR"),
])
def test_price_non_usd_is_none(extracted, text, currency):
    assert parse_price(extracted, text, currency) is None


def test_price_us_dollar_marker_ok():
    assert parse_price(25, "US$25.00", "$") == 25.0


# ---------- normalize_url ----------

def test_url_strips_tracking_params_and_fragment():
    url = "https://WWW.Example.com/p/lamp?id=7&utm_source=g&utm_medium=x&gclid=1&srsltid=abc&fbclid=f#reviews"
    assert normalize_url(url) == "https://www.example.com/p/lamp?id=7"


def test_url_keeps_other_params():
    assert normalize_url("https://www.ebay.com/itm/1?_skw=lamp&hash=x") == "https://www.ebay.com/itm/1?_skw=lamp&hash=x"


def test_url_keeps_google_query_string():
    url = "https://www.google.com/search?ibp=oshop&q=lamp&prds=catalogid:1&srsltid=zz#frag"
    assert normalize_url(url) == "https://www.google.com/search?ibp=oshop&q=lamp&prds=catalogid:1&srsltid=zz"


# ---------- dedupe ----------

def test_dedupe_same_url_keeps_lens_copy():
    lens = product(url="https://shop.example/p/1?utm_source=lens", source="lens", store="West Elm")
    shop = product(url="https://SHOP.example/p/1", source="shopping", store="West Elm (Shopping)")
    assert dedupe([lens, shop]) == [lens]


def test_dedupe_same_store_near_identical_title_keeps_first():
    a = product(url="https://a.example/1", title="Tripod Floor Lamp, Walnut Finish")
    b = product(url="https://a.example/2", title="Tripod Floor Lamp - Walnut Finish!")
    assert dedupe([a, b]) == [a]


def test_dedupe_keeps_same_title_at_different_stores():
    a = product(url="https://a.example/1", store="Target")
    b = product(url="https://b.example/1", store="Walmart")
    assert dedupe([a, b]) == [a, b]


def test_dedupe_keeps_different_google_products():
    a = product(url="https://www.google.com/search?ibp=oshop&prds=catalogid:1", store="A")
    b = product(url="https://www.google.com/search?ibp=oshop&prds=catalogid:2", store="B")
    assert dedupe([a, b]) == [a, b]


# ---------- parsers (real saved responses, plus documented shapes) ----------

def test_parse_shopping_real_response():
    products, dropped = parse_shopping(raw("shopping_rice_lamp.json"))
    first = raw("shopping_rice_lamp.json")["shopping_results"][0]
    assert dropped == 0 and len(products) == 6
    assert products[0].url == first["product_link"]  # live results have no direct link
    assert products[0].store == first["source"] and products[0].price == first["extracted_price"]
    assert [p.source_rank for p in products] == list(range(6))
    assert all(p.source == "shopping" and p.image_url for p in products)


def test_parse_shopping_prefers_direct_link():
    data = {"shopping_results": [{"title": "Lamp", "source": "Target", "extracted_price": 20, "price": "$20.00",
                                  "link": "https://target.example/lamp", "product_link": "https://www.google.com/x"}]}
    assert parse_shopping(data)[0][0].url == "https://target.example/lamp"


def test_parse_ebay_real_response():
    products, dropped = parse_ebay(raw("ebay_rice_lamp.json"))
    assert dropped == 0 and len(products) == 6
    assert all(p.store == "eBay" and p.tier == "new" for p in products)  # all "Brand New"
    assert products[-1].price == 49.99  # the {"from", "to"} range takes its low end


@pytest.mark.parametrize("condition, tier", [
    ("Pre-Owned", "used"), ("Used", "used"), ("Seller refurbished", "used"),
    ("Certified - Refurbished", "used"), ("Brand New", "new"), ("Open Box", "new"),
])
def test_parse_ebay_tier(condition, tier):
    data = {"organic_results": [{"title": "Lamp", "link": "https://www.ebay.com/itm/1", "condition": condition,
                                 "price": {"raw": "$30.00", "extracted": 30.0}}]}
    assert parse_ebay(data)[0][0].tier == tier


def test_parse_lens_drops_unpriced_and_non_usd():
    data = {"visual_matches": [
        {"position": 1, "title": "Lamp A", "link": "https://a.example", "source": "Etsy", "thumbnail": "t1",
         "price": {"value": "$45.00*", "extracted_value": 45, "currency": "$"}},
        {"position": 2, "title": "Lamp B", "link": "https://b.example", "source": "Yahoo! JAPAN",
         "price": {"value": "¥170,280*", "extracted_value": 170280, "currency": "¥"}},
        {"position": 3, "title": "Lamp C", "link": "https://c.example", "source": "Pinterest"},
    ]}
    products, dropped = parse_lens(data)
    assert [(p.title, p.price, p.store, p.source_rank) for p in products] == [("Lamp A", 45.0, "Etsy", 0)]
    assert dropped == 2


def test_parse_lens_used_condition():
    data = {"visual_matches": [
        {"title": "Vintage Lamp", "link": "https://1stdibs.example/1", "source": "1stDibs", "condition": "Used",
         "price": {"value": "$1,125*", "extracted_value": 1125.0, "currency": "$"}},
        {"title": "New Lamp", "link": "https://target.example/1", "source": "Target",
         "price": {"value": "$90*", "extracted_value": 90.0, "currency": "$"}},
    ]}
    assert [p.tier for p in parse_lens(data)[0]] == ["used", "new"]


def test_parse_lens_no_results_response():
    assert parse_lens(raw("lens_no_results.json")) == ([], 0)


# ---------- queries_from_lens ----------

def test_queries_from_lens_cleans_titles_and_skips_dupes():
    titles = ["Rattan Arc Floor Lamp - Wayfair", "rattan arc floor lamp | Etsy", "Lamp", "Brass Tripod Floor Lamp"]
    assert queries_from_lens("floor lamp", titles) == ["Rattan Arc Floor Lamp", "Brass Tripod Floor Lamp"]


def test_queries_from_lens_puts_label_in_front_when_missing():
    assert queries_from_lens("lamp", ["Mid-Century Brass Fixture"]) == ["lamp Mid-Century Brass Fixture"]


def test_queries_from_lens_limit_and_word_cap():
    titles = [f"Walnut Side Table Model {i} with a very long marketing description attached" for i in range(5)]
    qs = queries_from_lens("table", titles)
    assert len(qs) == 3 and all(len(q.split()) <= 8 for q in qs)


@pytest.mark.parametrize("titles", [[], ["Lamp"], ["  "]])
def test_queries_from_lens_falls_back_to_label(titles):
    assert queries_from_lens("table lamp", titles) == ["table lamp"]


def test_queries_from_lens_nothing_at_all():
    assert queries_from_lens("", []) == []


# ---------- search_item / search_room ----------

LENS_RAW = {"visual_matches": [
    {"title": "White Rice Paper Table Lamp - Wayfair", "link": "https://w.example/1", "source": "Wayfair"},  # no price
    {"title": "Paper Lantern Lamp", "link": "https://w.example/2", "source": "Etsy",
     "price": {"value": "$40.00", "extracted_value": 40.0, "currency": "$"}},
]}


def fake_serp(lens_behavior, seen=None):
    async def serp(params, capture_id=None, name=None):
        if seen is not None:
            seen.append((params["engine"], params.get("q") or params.get("_nkw")))
        if params["engine"] == "google_lens":
            return await lens_behavior()
        if params["engine"] == "ebay":
            return raw("ebay_rice_lamp.json")
        data = raw("shopping_rice_lamp.json")
        if name == "shopping_1":  # make query 1's products distinct so dedupe keeps them
            for r in data["shopping_results"]:
                r["product_link"] += "&q1"
                r["source"] += " (q1)"
        return data
    return serp


async def lens_ok():
    return LENS_RAW


def test_search_item_uses_lens_titles_as_queries_even_when_unpriced(monkeypatch):
    seen = []
    monkeypatch.setattr(search, "serp", fake_serp(lens_ok, seen))
    products, stats, queries = asyncio.run(search.search_item_with_stats("https://img.example/c.jpg", "table lamp"))
    assert queries == ["White Rice Paper Table Lamp", "table lamp Paper Lantern Lamp"]
    assert ("google_shopping", queries[0]) in seen and ("google_shopping", queries[1]) in seen
    assert ("ebay", queries[0]) in seen
    assert stats["lens"]["dropped_no_price"] == 1
    assert {p.source for p in products} == {"lens", "shopping", "ebay"}
    shop1 = [p.source_rank for p in products if p.store.endswith("(q1)")]
    assert shop1 == list(range(6, 12))  # query 1 continues query 0's numbering


def test_search_item_survives_failing_lens(monkeypatch):
    async def lens_down():
        raise search.SerpError("HTTP 401: Invalid API key")

    seen = []
    monkeypatch.setattr(search, "serp", fake_serp(lens_down, seen))
    products = asyncio.run(search.search_item("https://img.example/c.jpg", "table lamp"))
    assert {p.source for p in products} == {"shopping", "ebay"}
    assert ("google_shopping", "table lamp") in seen  # fell back to the SAM label


def test_search_item_survives_slow_lens(monkeypatch):
    async def lens_hangs():
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(search, "serp", fake_serp(lens_hangs))
    monkeypatch.setitem(search.TIMEOUT_S, "lens", 0.05)
    products, stats, _ = asyncio.run(search.search_item_with_stats("https://img.example/c.jpg", "table lamp"))
    assert stats["lens"]["status"].startswith("timeout")
    assert {p.source for p in products} == {"shopping", "ebay"}


def test_search_all_wrapper_uses_category_as_label(monkeypatch):
    seen = []
    monkeypatch.setattr(search, "serp", fake_serp(lens_ok, seen))
    asyncio.run(search.search_all("https://img.example/c.jpg", Item(category="table lamp")))
    assert any(e == "google_lens" for e, _ in seen)


def test_search_room_one_failing_item_does_not_affect_others(monkeypatch):
    async def fake_item(url, label, capture_id=None):
        if label == "bad":
            raise RuntimeError("boom")
        return [product(title=label)]

    monkeypatch.setattr(search, "search_item", fake_item)
    done = []

    async def on_result(i, label, products):
        done.append((i, label, len(products)))

    items = [("lamp", "https://i/1"), ("bad", "https://i/2"), ("chair", "https://i/3")]
    results = asyncio.run(search.search_room(items, "c_1", on_result))
    assert [len(r) for r in results] == [1, 0, 1]
    assert sorted(done) == [(0, "lamp", 1), (1, "bad", 0), (2, "chair", 1)]


def test_search_room_caps_items_and_concurrency(monkeypatch):
    running = peak = 0

    async def fake_item(url, label, capture_id=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return []

    monkeypatch.setattr(search, "search_item", fake_item)
    results = asyncio.run(search.search_room([(f"item{i}", f"https://i/{i}") for i in range(8)]))
    assert len(results) == search.MAX_ROOM_ITEMS
    assert peak <= search.ROOM_CONCURRENCY
