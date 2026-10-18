"""Mocked tests for rerank, pricing and rank. No network, no API key.

    python -m unittest test_ranking -v        (from backend/)
"""
import asyncio
import json
import logging
import unittest
from unittest import mock

import anthropic
import httpx

import config
import pricing
import rank
import rerank
from models import Item, PriceInsight, Product, SearchResponse

logging.getLogger("gtl").setLevel(logging.ERROR)  # the failure tests log expected warnings


def fixture(name: str):
    return json.loads((config.FIXTURES / name).read_text().replace("__BASE__", config.PUBLIC_BASE_URL))


RAW = [Product.model_validate(p) for p in fixture("products_raw_lamp.json")]
RANKED = [Product.model_validate(p) for p in fixture("products_ranked_lamp.json")]
ITEM = Item.model_validate(fixture("item_lamp.json"))
EMBED_REQUEST = fixture("embed_service_request.json")
EXAMPLE = SearchResponse.model_validate(fixture("search_response.json"))
INSIGHT = pricing.compute_price_insight(RANKED)

BY_TITLE = {p.title: p for p in RAW}
WEST_ELM = BY_TITLE["Mid-Century Tripod Floor Lamp"].url              # $349, score 92
WAYFAIR = BY_TITLE["Walnut Tripod Floor Lamp with Linen Shade"].url   # $229.99, 84
TARGET = BY_TITLE["Tripod Floor Lamp, Walnut Finish"].url             # $89.99, 84
AMAZON = BY_TITLE["Wood Tripod Floor Lamp"].url                       # $119.99, 76
ARTICLE = BY_TITLE["Walnut Wood Tripod Lamp"].url                     # $279, 73
POTTERY = BY_TITLE["Tripod Floor Lamp with Drum Shade"].url           # $399, 78
EBAY = BY_TITLE["West Elm Mid-Century Tripod Floor Lamp - Pre-owned"].url  # used $120, 78
VINTAGE = BY_TITLE["Vintage Walnut Tripod Floor Lamp"].url            # used $95, 62
WOOD_LEGS = BY_TITLE["Tripod Floor Lamp Wood Legs - Used"].url        # used $40, dropped by rerank


def dump(products: list[Product]) -> list[dict]:
    return [p.model_dump() for p in products]


def product(price: float, score: int | None = None, tier: str = "new", n: int = 0) -> Product:
    return Product(title=f"Lamp {n}", store="Store", price=price, url=f"https://example.com/{n}",
                   source="ebay" if tier == "used" else "shopping", tier=tier, source_rank=n,
                   match_score=score)


# ---------- rerank ----------

class RerankTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher = mock.patch.object(rerank, "EMBED_URL", "http://embed.test")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def rerank(self, products, handler=None):
        transport = httpx.MockTransport(handler) if handler else rerank.fixture_transport()
        async with httpx.AsyncClient(transport=transport) as client:
            return await rerank.rerank(EMBED_REQUEST["query_url"], products, client=client)

    async def test_fixture_gives_exact_ranked_output(self):
        self.assertEqual(dump(await self.rerank(RAW)), dump(RANKED))

    async def test_sends_the_fixture_request(self):
        sent, inner = [], rerank.fixture_transport()

        def handler(request):
            sent.append(request)
            return inner.handler(request)
        for token in ("s3cret", ""):  # no token (Baptiste's server has no auth): no header at all
            with mock.patch.object(config, "MODEL_SERVICE_TOKEN", token):
                await self.rerank(RAW, handler)
        self.assertEqual(len(sent), 2)
        self.assertEqual(str(sent[0].url), "http://embed.test/embed")
        self.assertEqual(json.loads(sent[0].content), EMBED_REQUEST)
        self.assertEqual(sent[0].headers["authorization"], "Bearer s3cret")
        self.assertNotIn("authorization", sent[1].headers)

    async def test_maps_similarities_by_url_not_position(self):
        out = await self.rerank(RAW[::-1])
        self.assertEqual({p.url: p.match_score for p in out}, {p.url: p.match_score for p in RANKED})
        scores = [p.match_score for p in out if p.match_score is not None]
        self.assertEqual(scores, sorted(scores, reverse=True))

    async def test_only_top_25_by_source_rank_are_sent(self):
        many = [product(10.0, n=n).model_copy(update={"image_url": f"https://img/{n}", "source_rank": 29 - n})
                for n in range(30)]
        sent = []

        def handler(request):
            urls = json.loads(request.content)["candidate_urls"]
            sent.append(urls)
            return httpx.Response(200, json={"similarities": [0.9] * len(urls), "latency_ms": 5})
        out = await self.rerank(many, handler)
        self.assertEqual(sent[0], [p.image_url for p in many if p.source_rank < 25])  # input order
        self.assertEqual([p.source_rank for p in out if p.match_score is None], [29, 28, 27, 26, 25])

    async def test_failures_return_input_order_unscored(self):
        def raises(exc):
            def handler(request):
                raise exc("boom", request=request)
            return handler

        async def slow(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=fixture("embed_service_response.json"))

        def responds(*args, **kwargs):
            return lambda request: httpx.Response(*args, **kwargs)

        cases = {
            "read timeout": raises(httpx.ReadTimeout),
            "slow service": slow,
            "connection refused": raises(httpx.ConnectError),
            "HTTP 500": responds(500, text="boom"),
            "HTTP 404": responds(404, json={"detail": "Not Found"}),
            "bad JSON": responds(200, text="<html>not json</html>"),
            "wrong shape": responds(200, json={"scores": [0.9]}),
            "wrong length": responds(200, json={"similarities": [0.9, 0.8], "latency_ms": 1}),
            "non-numeric": responds(200, json={"similarities": ["high"] * 16, "latency_ms": 1}),
        }
        already_scored = [p.model_copy(update={"match_score": 99}) for p in RAW]
        unscored = [p.model_copy(update={"match_score": None}) for p in RAW]
        with mock.patch.object(rerank, "TIMEOUT_S", 0.05):
            for name, handler in cases.items():
                with self.subTest(name):
                    self.assertEqual(dump(await self.rerank(already_scored, handler)), dump(unscored))

    async def test_no_url_configured_makes_no_request(self):
        sent = []
        with mock.patch.object(rerank, "EMBED_URL", ""):
            out = await self.rerank(RAW, lambda request: sent.append(request))
        self.assertEqual(sent, [])
        self.assertEqual(dump(out), dump([p.model_copy(update={"match_score": None}) for p in RAW]))

    async def test_no_url_keeps_scores_search_set_best_first(self):
        scores = [55, None, 30, 90] + [None] * (len(RAW) - 4)
        scored = [p.model_copy(update={"match_score": s}) for p, s in zip(RAW, scores)]
        sent = []
        with mock.patch.object(rerank, "EMBED_URL", ""):
            out = await self.rerank(scored, lambda request: sent.append(request))
        self.assertEqual(sent, [])
        self.assertEqual([p.match_score for p in out[:2]], [90, 55])
        self.assertEqual([p.url for p in out[2:-1]], [p.url for p in scored if p.match_score is None])
        self.assertEqual(out[-1].match_score, 30)  # under MIN_SCORE: last, not dropped

    async def test_a_crop_in_memory_keeps_scores_search_set(self):
        # a background Look's crop (bytes) has no URL the CLIP service could fetch: as if there were none
        scores = [55, None, 30, 90] + [None] * (len(RAW) - 4)
        scored = [p.model_copy(update={"match_score": s}) for p, s in zip(RAW, scores)]
        sent = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: sent.append(request))) as client:
            out = await rerank.rerank(b"\xff\xd8 a jpeg", scored, client=client)
        self.assertEqual(sent, [])
        self.assertEqual([p.match_score for p in out[:2]], [90, 55])
        self.assertEqual(out[-1].match_score, 30)

    async def test_no_thumbnails_or_no_products(self):
        no_thumbs = [p.model_copy(update={"image_url": None}) for p in RAW]
        self.assertEqual(dump(await self.rerank(no_thumbs, lambda r: 1 / 0)), dump(no_thumbs))
        self.assertEqual(await self.rerank([], lambda r: 1 / 0), [])

    async def test_all_null_similarities(self):
        def handler(request):
            n = len(json.loads(request.content)["candidate_urls"])
            return httpx.Response(200, json={"similarities": [None] * n, "latency_ms": 1})
        self.assertEqual(dump(await self.rerank(RAW, handler)), dump(RAW))


# ---------- pricing ----------

class PricingTest(unittest.TestCase):
    def numbers(self, insight: PriceInsight):
        return insight.typical_low, insight.typical_high, insight.median, insight.sample_size

    def test_fixture_matches_example_response(self):
        insight = pricing.compute_price_insight(RANKED)
        self.assertEqual(self.numbers(insight), (119.99, 329.0, 265.0, 9))
        self.assertEqual(self.numbers(insight), self.numbers(EXAMPLE.price_insight))
        self.assertEqual((insight.best_match_verdict, insight.verdict_text), ("fair", ""))

    def test_used_listings_never_count(self):
        cheap_used = product(1.0, 99, "used", n=99)
        self.assertEqual(self.numbers(pricing.compute_price_insight(RANKED + [cheap_used])),
                         (119.99, 329.0, 265.0, 9))

    def test_fewer_than_4_close_matches_uses_all_new(self):
        products = [product(100, 90, n=1), product(200, 80, n=2), product(300, 60, n=3),
                    product(50, None, n=4), product(400, 30, n=5), product(10, 99, "used", n=6)]
        self.assertEqual(self.numbers(pricing.compute_price_insight(products)), (100.0, 300.0, 200.0, 5))

    def test_no_prices(self):
        for products in ([], [product(120, 80, "used")]):
            with self.subTest(len(products)):
                insight = pricing.compute_price_insight(products)
                self.assertEqual(self.numbers(insight), (None, None, None, 0))
                self.assertEqual(insight.best_match_verdict, "fair")

    def test_one_price(self):
        self.assertEqual(self.numbers(pricing.compute_price_insight([product(149.5, 80)])),
                         (149.5, 149.5, 149.5, 1))

    def test_two_prices_stay_inside_observed_range(self):
        products = [product(100, 90, n=1), product(200, 90, n=2)]
        self.assertEqual(self.numbers(pricing.compute_price_insight(products)), (125.0, 175.0, 150.0, 2))

    def test_rounds_to_cents(self):
        products = [product(p, 90, n=i) for i, p in enumerate([10, 20.333, 30, 40.111])]
        insight = pricing.compute_price_insight(products)
        self.assertEqual((insight.typical_low, insight.typical_high), (17.75, 32.53))
        self.assertEqual(insight.median, round(insight.median, 2))


# ---------- rank ----------

def pick(url: str, why: str = "Looks alike", products: list[Product] = RANKED) -> dict:
    """A model pick of the product with this url, by its id in the shortlist Claude sees."""
    return {"id": [p.url for p in rank.shortlist(products)].index(url), "match_reason": why}


def reply(**changes) -> str:
    """A well-formed model reply: the picks from fixtures/search_response.json, plus changes."""
    body = {
        "best_match": pick(WEST_ELM, "Same tripod base and drum shade"),
        "dupes": [pick(TARGET, "Same silhouette, lighter wood"),
                  pick(AMAZON, "Similar tripod legs and drum shade")],
        "used": [pick(EBAY, "Likely the same West Elm lamp")],
        "spoken_summary": "That's a West Elm tripod lamp, about 349 dollars. Target has a close lookalike for 90.",
    }
    return json.dumps(body | changes)


TEMPLATE = "That's a West Elm floor lamp, about 349 dollars. Target has a lookalike for 90."


class RankTest(unittest.IsolatedAsyncioTestCase):
    async def choose(self, *replies, products=RANKED, insight=INSIGHT) -> SearchResponse:
        with mock.patch.object(rank.vlm.haiku, "call", mock.AsyncMock(side_effect=list(replies))) as model:
            resp = await rank.choose(ITEM, products, insight)
        self.model = model
        self.check_rules(resp, products)
        return resp

    def check_rules(self, resp: SearchResponse, products: list[Product]):
        """What must hold for any response, whatever the model said."""
        ours = {p.url: p for p in products}
        best, picks = resp.best_match, [p for p in [resp.best_match, *resp.dupes, *resp.used] if p]
        self.assertEqual(len({p.url for p in picks}), len(picks), "a product appears twice")
        for p in picks:
            src = ours[p.url]
            self.assertEqual((p.title, p.store, p.price, p.match_score, p.image_url, p.source_rank),
                             (src.title, src.store, src.price, src.match_score, src.image_url, src.source_rank))
            self.assertLess(len(p.match_reason.split()), 12)
        if best:  # new, unless only a used listing is the item itself (match_score of FOUND_SCORE or more)
            used_best = ours[best.url].tier == "used"
            self.assertEqual(best.tier, "used" if used_best else "new")
            if used_best:
                self.assertGreaterEqual(best.match_score, rank.FOUND_SCORE)
                self.assertFalse(any(p.tier != "used" and (p.match_score or 0) >= rank.FOUND_SCORE for p in products))
        for d in resp.dupes:
            self.assertEqual((d.tier, ours[d.url].tier), ("dupe", "new"))
            self.assertLess(d.price, best.price)
        for u in resp.used:
            self.assertEqual((u.tier, ours[u.url].tier), ("used", "used"))
        for p in resp.dupes + resp.used:
            self.assertEqual(p.savings_pct, round((1 - p.price / best.price) * 100) if best else None)
        self.assertLessEqual(len(resp.dupes), 3)
        self.assertLessEqual(len(resp.used), 2)
        self.assertTrue(resp.spoken_summary == rank.NOT_FOUND or rank.summary_ok(resp.spoken_summary, picks),
                        resp.spoken_summary)
        self.assertEqual((resp.capture_id, resp.timing_ms), ("", {}))

    async def test_good_reply_matches_example_response(self):
        resp = await self.choose(reply())
        self.assertEqual(resp.best_match, EXAMPLE.best_match)
        self.assertEqual(resp.dupes, EXAMPLE.dupes)
        self.assertEqual(resp.used, EXAMPLE.used)
        self.assertEqual(resp.spoken_summary, EXAMPLE.spoken_summary)
        self.assertEqual(resp.price_insight.best_match_verdict, "high")
        self.assertEqual(resp.price_insight.verdict_text,
                         "West Elm is above the typical $120–$329 range. The Target dupe saves you $259.")
        self.assertEqual(self.model.await_count, 1)

    async def test_made_up_ids_are_dropped(self):
        made_up = [{"id": 99, "match_reason": "Looks alike"}, {"id": -1, "match_reason": "Looks alike"}]
        resp = await self.choose(reply(best_match=made_up[0], dupes=[*made_up, pick(TARGET)]))
        self.assertEqual(resp.best_match.url, WEST_ELM)  # top re-ranked non-used product
        self.assertEqual(resp.best_match.match_reason, "Closest visual match")
        self.assertEqual([d.url for d in resp.dupes], [TARGET])
        self.assertEqual(resp.spoken_summary, TEMPLATE)  # the model's summary described its own pick

    async def test_used_best_match_is_replaced(self):
        resp = await self.choose(reply(best_match=pick(EBAY)))
        self.assertEqual(resp.best_match.url, WEST_ELM)
        self.assertEqual([u.url for u in resp.used], [EBAY])
        self.assertEqual(resp.spoken_summary, TEMPLATE)

    async def test_dupes_must_be_new_and_cheaper(self):
        resp = await self.choose(reply(dupes=[pick(POTTERY), pick(WEST_ELM), pick(VINTAGE), pick(TARGET)]))
        self.assertEqual([d.url for d in resp.dupes], [TARGET])  # $399, the best match, used: all dropped
        self.assertEqual(resp.spoken_summary, EXAMPLE.spoken_summary)

    async def test_bad_spoken_summary_is_replaced(self):
        cases = {
            "off-by-a-few price": "That's a West Elm lamp, about 345 dollars. Target has one for 90.",
            "unrounded price": "That's a West Elm lamp for 349 dollars. Target has one for 89.99.",
            "savings amount": "That's West Elm at 349 dollars. Target's lookalike saves you 259.",
            "dropped pick's price": "West Elm is 349 dollars. Pottery Barn has one for 399.",
            "30 words": " ".join(["word"] * 30),
            "empty": "",
        }
        for name, summary in cases.items():
            with self.subTest(name):
                resp = await self.choose(reply(spoken_summary=summary, dupes=[pick(POTTERY), pick(TARGET)]))
                self.assertEqual(resp.spoken_summary, TEMPLATE)

    async def test_texts_name_the_cheapest_dupe(self):
        resp = await self.choose(reply(dupes=[pick(WAYFAIR), pick(TARGET)], spoken_summary="Around 300."))
        self.assertEqual([d.url for d in resp.dupes], [WAYFAIR, TARGET])  # the model's order is kept
        self.assertEqual(resp.spoken_summary, TEMPLATE)
        self.assertTrue(resp.price_insight.verdict_text.endswith("The Target dupe saves you $259."))

    def test_summary_check(self):
        target, west_elm = BY_TITLE["Tripod Floor Lamp, Walnut Finish"], BY_TITLE["Mid-Century Tripod Floor Lamp"]
        self.assertTrue(rank.summary_ok("CB2 has a lookalike for $90.", [target]))  # "2" is part of a name
        self.assertTrue(rank.summary_ok("About 349 dollars, or 90 at Target.", [west_elm, target]))
        self.assertFalse(rank.summary_ok("About 349 dollars, 74% less at Target.", [west_elm, target]))

    async def test_invalid_json_twice_uses_fallback(self):
        resp = await self.choose("not json", '{"best_match": {"url": ')
        self.assertEqual(self.model.await_count, 2)
        self.assertEqual(resp.best_match.url, WEST_ELM)
        self.assertEqual(resp.best_match.match_reason, "Closest visual match")
        self.assertEqual([d.url for d in resp.dupes], [TARGET, AMAZON, WAYFAIR])  # cheapest, score >= 70
        self.assertEqual([u.url for u in resp.used], [EBAY, VINTAGE])           # best scores
        self.assertEqual(resp.spoken_summary, TEMPLATE)
        self.assertEqual(resp.price_insight.verdict_text,
                         "West Elm is above the typical $120–$329 range. The Target dupe saves you $259.")

    async def test_wrong_schema_or_api_errors_use_fallback(self):
        cases = {
            "wrong schema": ['{"best": "West Elm"}', '{"picks": []}'],
            "api errors": [anthropic.AnthropicError("overloaded"), anthropic.AnthropicError("overloaded")],
            "refusal": [ValueError("stop_reason=refusal"), ValueError("stop_reason=refusal")],
        }
        for name, replies in cases.items():
            with self.subTest(name):
                resp = await self.choose(*replies)
                self.assertEqual(self.model.await_count, 2)
                self.assertEqual(resp.best_match.url, WEST_ELM)
                self.assertEqual(resp.spoken_summary, TEMPLATE)

    async def test_retry_after_one_bad_reply(self):
        resp = await self.choose("Sure! Here's the JSON you asked for:", reply())
        self.assertEqual(self.model.await_count, 2)
        self.assertEqual(resp.spoken_summary, EXAMPLE.spoken_summary)

    async def test_no_retry_once_budget_is_spent(self):
        async def slow_failure(system, content, schema, max_tokens, timeout):
            await asyncio.sleep(0.2)
            raise anthropic.AnthropicError("timeout")
        with mock.patch.object(rank, "BUDGET_S", 1.1), \
                mock.patch.object(rank.vlm.haiku, "call", mock.AsyncMock(side_effect=slow_failure)) as model:
            resp = await rank.choose(ITEM, RANKED, INSIGHT)
        self.assertEqual(model.await_count, 1)
        self.assertLessEqual(model.await_args.args[4], 1.1)
        self.assertEqual(resp.best_match.url, WEST_ELM)

    async def test_lists_are_capped_and_never_repeat(self):
        resp = await self.choose(reply(
            best_match=pick(WEST_ELM, products=RAW),
            dupes=[pick(url, products=RAW) for url in [TARGET, TARGET, WEST_ELM, AMAZON, WAYFAIR, ARTICLE]],
            used=[pick(url, products=RAW) for url in [EBAY, EBAY, VINTAGE, WOOD_LEGS]],
        ), products=RAW)
        self.assertEqual([d.url for d in resp.dupes], [TARGET, AMAZON, WAYFAIR])
        self.assertEqual([u.url for u in resp.used], [EBAY, VINTAGE])

    async def test_long_reason_is_cut(self):
        long = "Same walnut tripod legs, same linen drum shade, same height, same brass switch, same everything"
        resp = await self.choose(reply(best_match=pick(WEST_ELM, long)))
        self.assertEqual(resp.best_match.match_reason, " ".join(long.split()[:11]))

    def test_cut_reason_drops_dangling_words(self):
        cases = {
            "Walnut tripod with linen shade matches the description closely at a fair price":
                "Walnut tripod with linen shade matches the description closely",
            "Same lamp, same shade, same walnut, same legs, same height, and cheaper":
                "Same lamp, same shade, same walnut, same legs, same height",
            "Same tripod base and drum shade": "Same tripod base and drum shade",  # short: untouched
            "": "Fallback",
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(rank.reason(text, "Fallback"), expected)

    async def test_verdicts(self):
        def insight(low, high, n):
            return INSIGHT.model_copy(update={"typical_low": low, "typical_high": high, "sample_size": n})
        cases = [
            (insight(119.99, 329.0, 9), "high", "West Elm is above the typical $120–$329 range."),
            (insight(400.0, 600.0, 9), "low", "West Elm is below the typical $400–$600 range."),
            (insight(300.0, 400.0, 9), "fair", "West Elm is within the typical $300–$400 range."),
            (insight(100.0, 200.0, 3), "fair", "Not enough comparable prices to judge this one."),
        ]
        for ins, verdict, text in cases:
            with self.subTest(verdict=verdict, n=ins.sample_size):
                resp = await self.choose(reply(), insight=ins)
                self.assertEqual(resp.price_insight.best_match_verdict, verdict)
                self.assertEqual(resp.price_insight.verdict_text, f"{text} The Target dupe saves you $259.")
                self.assertEqual(resp.price_insight.typical_low, ins.typical_low)

    async def test_no_products(self):
        resp = await self.choose(products=[])
        self.assertEqual(self.model.await_count, 0)
        self.assertIsNone(resp.best_match)
        self.assertEqual((resp.dupes, resp.used), ([], []))
        self.assertEqual(resp.spoken_summary, "I couldn't find that one for sale.")
        self.assertEqual(resp.price_insight, INSIGHT)

    async def test_only_used_products(self):
        used = [p for p in RANKED if p.tier == "used"]
        resp = await self.choose(json.dumps({
            "best_match": pick(EBAY, products=used), "dupes": [],
            "used": [pick(EBAY, products=used), pick(VINTAGE, products=used)],
            "spoken_summary": "A used West Elm one is 120 dollars on eBay.",
        }), products=used)
        self.assertIsNone(resp.best_match)
        self.assertEqual([u.url for u in resp.used], [EBAY, VINTAGE])
        self.assertEqual(resp.spoken_summary, "I found a used one on eBay for 120 dollars.")
        self.assertEqual((resp.price_insight.best_match_verdict, resp.price_insight.verdict_text), ("fair", ""))

    async def test_unscored_products_fallback(self):
        """rerank failed: nothing has a score, so no fallback dupe qualifies."""
        unscored = [p.model_copy(update={"match_score": None}) for p in RAW]
        resp = await self.choose("{}", "{}", products=unscored, insight=pricing.compute_price_insight(unscored))
        self.assertEqual((resp.best_match.url, resp.best_match.match_reason), (WEST_ELM, "Top search result"))
        self.assertEqual(resp.dupes, [])
        self.assertEqual([u.url for u in resp.used], [EBAY, VINTAGE])
        self.assertEqual(resp.spoken_summary,
                         "That's a West Elm floor lamp, about 349 dollars. There's a used one on eBay for 120.")

    async def test_prompt_sends_only_the_listed_fields(self):
        await self.choose(reply())
        user = self.model.await_args.args[1]  # (system, user, schema, max_tokens, timeout)
        item_line, insight_line, results_line = user.splitlines()
        self.assertNotIn("bbox", json.loads(item_line.removeprefix("Item: ")))
        self.assertEqual(json.loads(insight_line.removeprefix("Price insight: ")),
                         {"typical_low": 119.99, "typical_high": 329.0, "median": 265.0, "sample_size": 9})
        results = json.loads(results_line.removeprefix("Results: "))
        self.assertEqual([r["id"] for r in results], list(range(len(RANKED))))
        for r in results:
            self.assertEqual(set(r), rank.FIELDS | {"id"})  # no URLs or image links

    def test_shortlist_keeps_room_for_used_listings(self):
        many_new = [product(100.0 + n, 90, n=n) for n in range(20)]
        used = [product(50.0, 50, "used", n=100 + n) for n in range(5)]
        self.assertEqual([p.tier for p in rank.shortlist(used[:2] + many_new + used[2:])],
                         ["new"] * 12 + ["used"] * 3)

    @staticmethod
    def best_pick(id: int) -> str:
        return json.dumps({"best_match": {"id": id, "match_reason": "Same bottle"}, "dupes": [], "used": [],
                           "spoken_summary": "That's a Store floor lamp, about 300 dollars."})

    async def test_low_match_score_gives_the_closest_not_exact(self):
        products = [product(300.0, 60, n=0), product(200.0, 70, n=1), product(100.0, 40, n=2)]
        resp = await self.choose(self.best_pick(0), products=products)
        self.assertEqual(resp.best_match.url, products[1].url)  # highest score, though under FOUND_SCORE
        self.assertEqual(resp.best_match.match_reason, rank.NOT_EXACT_REASON)
        self.assertTrue(resp.spoken_summary.startswith("I couldn't find this exact one."), resp.spoken_summary)
        self.assertTrue(resp.price_insight.verdict_text.startswith("Not the exact item."))
        self.assertEqual(resp.price_insight.best_match_verdict, "fair")

    async def test_low_scoring_pick_gives_way_to_a_found_match(self):
        products = [product(300.0, 60, n=0), product(200.0, 88, n=1)]
        resp = await self.choose(self.best_pick(0), products=products)
        self.assertEqual(resp.best_match.url, products[1].url)
        self.assertEqual(resp.best_match.match_reason, "Highest match score")
        self.assertNotIn("couldn't find", resp.spoken_summary)  # Claude's summary was about its own pick

    async def test_final_best_match_goes_through_store_link(self):
        async def linked(best):
            return best.model_copy(update={"url": "https://store.example/p"})

        with mock.patch.object(rank.vlm.haiku, "call", mock.AsyncMock(return_value=reply())), \
                mock.patch.object(rank.search, "with_store_link", linked):
            resp = await rank.choose(ITEM, RANKED, INSIGHT)
        self.assertEqual(resp.best_match.url, "https://store.example/p")
        self.assertEqual(resp.best_match.title, BY_TITLE["Mid-Century Tripod Floor Lamp"].title)  # Claude's pick

    async def test_only_a_used_listing_found_makes_it_the_match(self):
        products = [product(300.0, 60, n=0), product(120.0, 90, "used", n=1), product(200.0, 50, n=2)]
        resp = await self.choose(self.best_pick(0), products=products)
        self.assertEqual((resp.best_match.url, resp.best_match.tier), (products[1].url, "used"))
        self.assertEqual(resp.best_match.match_reason, rank.USED_MATCH_REASON)
        self.assertEqual(resp.spoken_summary, "That's a Store floor lamp, used, about 120 dollars.")

    async def test_unchecked_pick_is_not_found_when_the_checked_ones_missed(self):
        # match.py scored only some results, all low; Claude's pick is one it never checked
        products = [product(300.0, None, n=0), product(200.0, 30, n=1), product(250.0, None, n=2)]
        resp = await self.choose(self.best_pick(0), products=products)
        self.assertEqual(resp.best_match.match_reason, rank.NOT_EXACT_REASON)
        self.assertTrue(resp.spoken_summary.startswith("I couldn't find this exact one."), resp.spoken_summary)

    async def test_found_match_keeps_claudes_pick_and_summary(self):
        products = [product(300.0, 80, n=0), product(200.0, 95, n=1)]
        resp = await self.choose(self.best_pick(0), products=products)
        self.assertEqual((resp.best_match.url, resp.best_match.match_reason), (products[0].url, "Same bottle"))
        self.assertEqual(resp.spoken_summary, "That's a Store floor lamp, about 300 dollars.")


if __name__ == "__main__":
    unittest.main()
