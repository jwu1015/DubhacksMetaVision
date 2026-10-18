"""pytest tests/test_search.py  (from backend/). No network: SerpAPI calls are faked."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

import search
from models import Item, Product
from search import dedupe, normalize_url, parse_amazon, parse_ebay, parse_lens, parse_price, parse_shopping

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


def test_parse_shopping_second_hand_is_used():
    data = {"shopping_results": [
        {"title": "Lamp A", "product_link": "https://g.example/a", "extracted_price": 40.0},
        {"title": "Lamp B", "product_link": "https://g.example/b", "extracted_price": 30.0, "second_hand_condition": "pre-owned"},
        {"title": "Lamp C", "product_link": "https://g.example/c", "extracted_price": 20.0, "second_hand_condition": "refurbished"},
    ]}
    assert [p.tier for p in parse_shopping(data)[0]] == ["new", "used", "used"]


def test_parse_amazon_real_response():
    products, dropped = parse_amazon(raw("amazon_airpods_case.json"))
    assert len(products) == 22 and dropped == 0
    first = products[0]
    assert (first.source, first.store, first.price, first.source_rank) == ("amazon", "Amazon", 28.99, 0)
    assert first.url == "https://www.amazon.com/dp/B0H3P63L4W/"  # link_clean, not the sponsored ad redirect
    assert first.image_url.startswith("https://m.media-amazon.com/")
    renewed = [p for p in products if "renewed" in p.title.lower()]
    assert renewed and all(p.tier == "used" for p in renewed)


def test_parse_lens_no_results_response():
    assert parse_lens(raw("lens_no_results.json")) == ([], 0)


# ---------- search_all / search_room ----------

ITEM = Item(category="table lamp", description="White rice paper square table lamp on a wood base.",
            search_queries=["white rice paper square table lamp", "paper table lamp", "lamp"])


def fake_serp(seen=None, fail=()):
    async def serp(params, capture_id=None, name=None):
        if seen is not None:
            seen.append((params["engine"], params.get("q") or params.get("_nkw") or params.get("k")))
        if name in fail:
            raise search.SerpError("HTTP 401: Invalid API key")
        if params["engine"] == "ebay":
            return raw("ebay_rice_lamp.json")
        if params["engine"] == "amazon":
            return raw("amazon_airpods_case.json")
        data = raw("shopping_rice_lamp.json")
        if name == "shopping_1":  # make query 1's products distinct so dedupe keeps them
            for r in data["shopping_results"]:
                r["product_link"] += "&q1"
                r["source"] += " (q1)"
        return data
    return serp


@pytest.fixture
def scored(monkeypatch):
    """match.score passes products through unchanged and records each call."""
    calls = []

    async def score(cutout_url, item, products):
        calls.append((cutout_url, item, products))
        return products

    monkeypatch.setattr(search.match, "score", score)
    return calls


def test_search_all_uses_claudes_queries_with_shopping_and_amazon(monkeypatch, scored):
    seen = []
    monkeypatch.setattr(search, "serp", fake_serp(seen))
    products = asyncio.run(search.search_all("https://img.example/c.jpg", ITEM))
    q = ITEM.search_queries
    assert sorted(seen) == sorted([("google_shopping", q[0]), ("google_shopping", q[1]), ("amazon", q[0])])
    assert {p.source for p in products} == {"shopping", "amazon"}  # no Lens, no eBay
    shop1 = [p.source_rank for p in products if p.store.endswith("(q1)")]
    assert shop1 == list(range(6, 12))  # query 1 continues query 0's numbering
    assert scored == [("https://img.example/c.jpg", ITEM, products)]  # every result goes to match.score


def test_search_all_falls_back_to_the_sam_label(monkeypatch, scored):
    seen = []
    monkeypatch.setattr(search, "serp", fake_serp(seen))
    asyncio.run(search.search_all("https://img.example/c.jpg", Item(category="table lamp")))
    assert sorted(seen) == [("amazon", "table lamp"), ("google_shopping", "table lamp")]


def test_search_all_with_nothing_to_search_for(monkeypatch, scored):
    seen = []
    monkeypatch.setattr(search, "serp", fake_serp(seen))
    assert asyncio.run(search.search_all("https://img.example/c.jpg", Item(category=None))) == []
    assert seen == []


def test_search_all_survives_a_failing_source(monkeypatch, scored):
    monkeypatch.setattr(search, "serp", fake_serp(fail={"shopping_0"}))
    products, stats = asyncio.run(search.search_all_with_stats("https://img.example/c.jpg", ITEM))
    assert stats["shopping_0"]["status"].startswith("error")
    assert {p.source for p in products} == {"shopping", "amazon"}  # query 1 and Amazon still came back


def test_search_all_survives_a_slow_source(monkeypatch, scored):
    async def slow_amazon(params, capture_id=None, name=None):
        if params["engine"] == "amazon":
            await asyncio.sleep(5)
        return await fake_serp()(params, capture_id, name)

    monkeypatch.setattr(search, "serp", slow_amazon)
    monkeypatch.setitem(search.TIMEOUT_S, "amazon", 0.05)
    products, stats = asyncio.run(search.search_all_with_stats("https://img.example/c.jpg", ITEM))
    assert stats["amazon"]["status"].startswith("timeout")
    assert {p.source for p in products} == {"shopping"}


def test_search_all_returns_the_match_scores(monkeypatch):
    async def score(cutout_url, item, products):
        return [p.model_copy(update={"match_score": 80}) for p in products]

    monkeypatch.setattr(search, "serp", fake_serp())
    monkeypatch.setattr(search.match, "score", score)
    products = asyncio.run(search.search_all("https://img.example/c.jpg", ITEM))
    assert products and all(p.match_score == 80 for p in products)


def test_search_room_one_failing_item_does_not_affect_others(monkeypatch):
    async def fake_all(url, item, capture_id=None):
        if item.category == "bad":
            raise RuntimeError("boom")
        return [product(title=item.category)]

    monkeypatch.setattr(search, "search_all", fake_all)
    done = []

    async def on_result(i, item, products):
        done.append((i, item.category, len(products)))

    items = [(Item(category=c), f"https://i/{n}") for n, c in enumerate(["lamp", "bad", "chair"])]
    results = asyncio.run(search.search_room(items, "c_1", on_result))
    assert [len(r) for r in results] == [1, 0, 1]
    assert sorted(done) == [(0, "lamp", 1), (1, "bad", 0), (2, "chair", 1)]


def test_search_room_caps_items_and_concurrency(monkeypatch):
    running = peak = 0

    async def fake_all(url, item, capture_id=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return []

    monkeypatch.setattr(search, "search_all", fake_all)
    results = asyncio.run(search.search_room([(Item(category=f"item{i}"), f"https://i/{i}") for i in range(8)]))
    assert len(results) == search.MAX_ROOM_ITEMS
    assert peak <= search.ROOM_CONCURRENCY


# ---------- store link for a found product ----------

IMMERSIVE = raw("immersive_airpods_case.json")  # a real google_immersive_product response
STORE_LINK = IMMERSIVE["product_results"]["stores"][0]["link"]


def serp_with_tokens(seen, immersive=IMMERSIVE):
    """fake_serp, plus a seller-page token on every Shopping result and the seller page itself."""
    base = fake_serp()

    async def serp(params, capture_id=None, name=None):
        if params["engine"] == "google_immersive_product":
            seen.append(params["page_token"])
            if isinstance(immersive, Exception):
                raise immersive
            return immersive
        data = await base(params, capture_id, name)
        for i, r in enumerate(data.get("shopping_results") or []):
            r["immersive_product_page_token"] = f"{name}-tok{i}"
        return data
    return serp


def score_one(source="shopping", top=90):
    """match.score that gives the first product from `source` the score `top`, and every other one 50."""
    async def score(cutout_url, item, products):
        first = next(p for p in products if p.source == source)
        return [p.model_copy(update={"match_score": top if p is first else 50}) for p in products]
    return score


def best(products):
    return max(products, key=lambda p: p.match_score or 0)


def searched_best(monkeypatch, seen, immersive=IMMERSIVE, source="shopping", score=90):
    """Run search_all on the fixtures (which records the seller-page tokens) and return its best result."""
    monkeypatch.setattr(search, "serp", serp_with_tokens(seen, immersive))
    monkeypatch.setattr(search.match, "score", score_one(source, score))
    products = asyncio.run(search.search_all("https://img.example/c.jpg", ITEM))
    assert seen == []  # search never fetches store links itself: rank does, for its final pick
    return best(products)


def test_found_shopping_match_gets_its_direct_store_link(monkeypatch):
    seen = []
    p = searched_best(monkeypatch, seen)
    assert asyncio.run(search.with_store_link(p)).url == STORE_LINK
    assert len(seen) == 1 and seen[0].startswith("shopping_0-tok")  # one more search


def test_not_found_means_no_extra_search(monkeypatch):
    seen = []
    p = searched_best(monkeypatch, seen, score=search.match.FOUND_SCORE - 1)
    assert asyncio.run(search.with_store_link(p)) == p and seen == []


def test_found_amazon_match_keeps_its_listing_link(monkeypatch):
    seen = []
    p = searched_best(monkeypatch, seen, source="amazon")
    assert asyncio.run(search.with_store_link(p)) == p and seen == []
    assert p.url.startswith("https://www.amazon.com/")


def test_store_link_failure_keeps_the_shopping_link(monkeypatch):
    seen = []
    p = searched_best(monkeypatch, seen, immersive=search.SerpError("HTTP 500"))
    assert asyncio.run(search.with_store_link(p)) == p and len(seen) == 1


def test_no_best_match_no_store_link():
    assert asyncio.run(search.with_store_link(None)) is None


def test_store_link_prefers_the_listing_store(monkeypatch):
    stores = [{"name": "Other Shop", "link": "https://other.example/p"},
              {"name": "Target", "link": "https://target.example/p"}]
    page = {"product_results": {"stores": stores}}
    p = Product(title="Lamp", store="target", price=50.0, url="https://www.google.com/x", source="shopping",
                source_rank=0)
    monkeypatch.setitem(search.PAGE_TOKENS, p.url, "tok")

    async def serp(params, capture_id=None, name=None):
        return page

    monkeypatch.setattr(search, "serp", serp)
    assert asyncio.run(search.store_link(p)) == "https://target.example/p"
    page["product_results"]["stores"] = stores[:1]
    assert asyncio.run(search.store_link(p)) == "https://other.example/p"  # no match: the first store


@pytest.mark.parametrize("took, expected", [
    (0.0, "search lens: SerpAPI processing 0.0s, our request"),
    (13.8, "served from SerpAPI's cache (first run took 13.8s)"),  # answered faster than it was made
    (None, "(SerpAPI gave no processing time)"),
])
def test_serp_logs_serpapi_processing_time(monkeypatch, caplog, took, expected):
    meta = {} if took is None else {"total_time_taken": took}
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"search_metadata": meta})))
    monkeypatch.setattr(search.net, "client", lambda: client)
    monkeypatch.setenv("SERP_DEV_CACHE", "false")

    async def go():
        try:
            await search.serp({"engine": "google_lens", "url": "https://img.example/x.jpg"}, name="lens")
        finally:
            await client.aclose()

    with caplog.at_level("INFO", logger="gtl.search"):
        asyncio.run(go())
    assert "search lens: " in caplog.text and expected in caplog.text
