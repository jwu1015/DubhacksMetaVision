"""pytest tests/test_search.py  (from backend/). No network: SerpAPI calls are faked."""
import asyncio
import json
from pathlib import Path

import pytest

import search
from models import Item, Product
from search import dedupe, normalize_url, parse_ebay, parse_lens, parse_price, parse_shopping

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


# ---------- search_all ----------

ITEM = Item(category="table lamp", search_queries=["white rice paper square table lamp", "paper table lamp", "lamp"])


def fake_serp(lens_behavior):
    async def serp(params, capture_id=None, name=None):
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


def test_search_all_survives_failing_source(monkeypatch):
    async def lens_down():
        raise search.SerpError("HTTP 401: Invalid API key")

    monkeypatch.setattr(search, "serp", fake_serp(lens_down))
    products = asyncio.run(search.search_all("https://img.example/cutout.jpg", ITEM))
    sources = {p.source for p in products}
    assert sources == {"shopping", "ebay"}
    shop1 = [p.source_rank for p in products if p.store.endswith("(q1)")]
    assert shop1 == list(range(6, 12))  # query 1 continues query 0's numbering


def test_search_all_survives_slow_source(monkeypatch):
    async def lens_hangs():
        await asyncio.sleep(5)
        return {}

    monkeypatch.setattr(search, "serp", fake_serp(lens_hangs))
    monkeypatch.setitem(search.TIMEOUT_S, "lens", 0.05)
    products, stats = asyncio.run(search.search_all_with_stats("https://img.example/cutout.jpg", ITEM))
    assert stats["lens"]["status"].startswith("timeout")
    assert {p.source for p in products} == {"shopping", "ebay"}
