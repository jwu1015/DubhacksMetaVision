"""W5: Claude picks the best match, dupes and used options; Python checks every pick.

    python rank.py fixtures/item_lamp.json fixtures/products_ranked_lamp.json

The model only picks products (by their id in the prompt) and writes words. Every price, score,
saving and verdict comes from our own Product data, and any pick that fails a check falls back
to a rule. Ids instead of URLs and Haiku instead of Sonnet keep the call near 2s: output length
is what costs time, and copying long store URLs back was most of it.
"""
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import anthropic
from pydantic import BaseModel

import config  # loads .env before the Anthropic client reads ANTHROPIC_API_KEY
import pricing
import search
from match import FOUND_SCORE  # the match score at which a result counts as the item itself
from models import Item, PriceInsight, Product, SearchResponse

log = logging.getLogger("gtl")

MODEL = "claude-haiku-5-5"
BUDGET_S = 4.0                # both attempts together; /search must finish in 5s
SHOW_NEW, SHOW_USED = 12, 3   # products sent to Claude, in re-ranked order; list index = id
MAX_DUPES, MAX_USED = 3, 2
REASON_WORDS = 11             # "under 12 words"
SUMMARY_WORDS = 29            # "under 30 words"
MIN_SAMPLE = 4                # fewer comparable prices than this: no verdict
FALLBACK_DUPE_SCORE = 70
NOT_FOUND = "I couldn't find that one for sale."
NOT_EXACT_REASON = "Closest match, not the exact item"
USED_MATCH_REASON = "Same item, pre-owned or refurbished"
DUPE_REASON, USED_REASON = "Similar look for less", "Pre-owned, similar look"
FIELDS = {"title", "store", "price", "match_score", "source", "tier"}  # plus "id"

SYSTEM = """You are a shopping assistant. Given an item a user saw in a room and a list
of product results already scored for visual similarity (match_score 0-100),
choose:
1. best_match: the single result most likely to be the SAME product or the
   closest match. Strongly prefer high match_score and reputable stores.
2. dupes: 2-3 NEW products that look similar (match_score >= 70 preferred)
   and cost noticeably less than best_match (ideally 30%+ cheaper).
3. used: up to 2 used listings that are close matches, if any.
Refer to products only by their id. Do not restate or change prices or scores.
Discard results that are clearly a different category.
For each pick, write a match_reason under 12 words.
Write spoken_summary: 1-2 short sentences, natural speech, whole dollars,
under 30 words, mentioning the best deal.
Return ONLY valid JSON:
{"best_match": {"id": 0, "match_reason": "..."},
 "dupes": [{"id": 0, "match_reason": "..."}],
 "used": [{"id": 0, "match_reason": "..."}],
 "spoken_summary": "..."}"""


class Pick(BaseModel):
    id: int
    match_reason: str


class Picks(BaseModel):
    best_match: Pick | None
    dupes: list[Pick]
    used: list[Pick]
    spoken_summary: str


OUTPUT_FORMAT = {"type": "json_schema", "schema": anthropic.transform_schema(Picks)}

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        # ask() retries once, within BUDGET_S
        _client = anthropic.AsyncAnthropic(max_retries=0, default_headers=config.ANTHROPIC_HEADERS)
    return _client


# ---------- the Claude call ----------

def shortlist(products: list[Product]) -> list[Product]:
    """The products Claude sees: top re-ranked new ones, plus a few used so they always get a look."""
    return ([p for p in products if p.tier != "used"][:SHOW_NEW]
            + [p for p in products if p.tier == "used"][:SHOW_USED])


def prompt(item: Item, shown: list[Product], insight: PriceInsight) -> str:
    numbers = insight.model_dump(include={"typical_low", "typical_high", "median", "sample_size"})
    results = [{"id": i, **p.model_dump(include=FIELDS)} for i, p in enumerate(shown)]
    return (f"Item: {json.dumps(item.model_dump(exclude={'bbox'}))}\n"
            f"Price insight: {json.dumps(numbers)}\n"
            f"Results: {json.dumps(results)}")


async def call_model(user: str, timeout: float) -> str:
    """One Claude call; returns the reply text."""
    msg = await client().messages.create(
        model=MODEL, max_tokens=1500, system=SYSTEM, timeout=timeout,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "disabled"},  # adaptive at low effort still thought here: 3.7s+ vs ~2.2s
        output_config={"effort": "low", "format": OUTPUT_FORMAT},
    )
    if msg.stop_reason != "end_turn":
        raise ValueError(f"stop_reason={msg.stop_reason}")
    return "".join(b.text for b in msg.content if b.type == "text")


async def ask(item: Item, shown: list[Product], insight: PriceInsight) -> Picks | None:
    """Claude's picks, or None after two failed attempts (or once the time budget runs out)."""
    user = prompt(item, shown, insight)
    deadline = time.perf_counter() + BUDGET_S
    for attempt in (1, 2):
        timeout = deadline - time.perf_counter()
        if timeout < 1.0:
            break
        t0 = time.perf_counter()
        try:
            picks = Picks.model_validate_json(await call_model(user, timeout))
            log.info("rank: claude %dms (attempt %d)", ms_since(t0), attempt)
            return picks
        except (anthropic.AnthropicError, ValueError) as e:  # ValueError covers bad JSON/schema
            log.warning("rank: claude attempt %d failed after %dms: %s: %s",
                        attempt, ms_since(t0), type(e).__name__, " ".join(str(e).split())[:200])
    return None


# ---------- checking the picks ----------

def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


def usd(x: float) -> str:
    return f"${round(x):,}"


DANGLING = {"a", "an", "the", "and", "or", "at", "with", "for", "of", "to", "in", "on", "but"}


def reason(text: str | None, default: str) -> str:
    """The model's reason cut to under 12 words, without a dangling 'at a' where it was cut."""
    words = (text or "").split()
    if len(words) > REASON_WORDS:
        words = words[:REASON_WORDS]
        while words and words[-1].lower().strip(",;:") in DANGLING:
            words.pop()
        words[-1:] = [w.rstrip(",;:") for w in words[-1:]]
    return " ".join(words) or default


def priced(p: Product, tier: str, why: str, best: Product | None) -> Product:
    """Our own Product with tier, reason and savings vs best set; nothing taken from the model."""
    savings = round((1 - p.price / best.price) * 100) if best and best.price > 0 else None
    return p.model_copy(update={"tier": tier, "match_reason": why, "savings_pct": savings})


NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")  # skips digits inside names like "CB2"


def summary_ok(text: str, picks: list[Product]) -> bool:
    """Short enough, and every number in it is the rounded price of one of our picks."""
    if not text.strip() or len(text.split()) > SUMMARY_WORDS:
        return False
    prices = {round(p.price) for p in picks}
    return all(float(n.replace(",", "")) in prices for n in NUMBER.findall(text))


def cheapest(products: list[Product]) -> Product:
    """The best deal, which is what the summary and verdict text mention."""
    return min(products, key=lambda p: p.price)


def a_or_an(word: str) -> str:
    return "an" if word[:1].lower() in tuple("aeiou") else "a"


def template_summary(item: Item, best: Product | None, dupes: list[Product], used: list[Product]) -> str:
    if best is None:
        if not used:
            return NOT_FOUND
        return f"I found a used one on {used[0].store} for {round(used[0].price)} dollars."
    condition = ", used," if best.tier == "used" else ","
    text = (f"That's {a_or_an(best.store)} {best.store} {item.category or 'item'}{condition} "
            f"about {round(best.price)} dollars.")
    if dupes:
        deal = cheapest(dupes)
        text += f" {deal.store} has a lookalike for {round(deal.price)}."
    elif used:
        text += f" There's a used one on {used[0].store} for {round(used[0].price)}."
    return text


def closest_summary(item: Item, best: Product) -> str:
    return (f"I couldn't find this exact one. The closest is {a_or_an(best.store)} {best.store} "
            f"{item.category or 'item'} for about {round(best.price)} dollars.")


def closest_text(insight: PriceInsight) -> str:
    if insight.sample_size < MIN_SAMPLE:
        return "Not the exact item."
    return f"Not the exact item. Similar ones usually cost {usd(insight.typical_low)}–{usd(insight.typical_high)}."


def verdict(best: Product | None, insight: PriceInsight) -> str:
    if best is None or insight.sample_size < MIN_SAMPLE:
        return "fair"
    if best.price < insight.typical_low:
        return "low"
    if best.price > insight.typical_high:
        return "high"
    return "fair"


def verdict_text(best: Product | None, dupes: list[Product], used: list[Product],
                 insight: PriceInsight, v: str) -> str:
    if best is None:
        return ""
    if insight.sample_size < MIN_SAMPLE:
        text = "Not enough comparable prices to judge this one."
    else:
        where = {"low": "below", "fair": "within", "high": "above"}[v]
        text = (f"{best.store} is {where} the typical "
                f"{usd(insight.typical_low)}–{usd(insight.typical_high)} range.")
    if dupes:
        deal = cheapest(dupes)
        text += f" The {deal.store} dupe saves you {usd(best.price - deal.price)}."
    elif deal := next((u for u in used if u.price < best.price), None):
        text += f" A used one on {deal.store} saves you {usd(best.price - deal.price)}."
    return text


def build(item: Item, products: list[Product], insight: PriceInsight, picks: Picks | None) -> SearchResponse:
    """Turn Claude's picks (or None, after two failures) into a checked SearchResponse."""
    shown = shortlist(products)
    new = [p for p in products if p.tier != "used"]

    def known(cs: list[Pick | None]) -> list[tuple[Product, str]]:  # drops made-up ids
        return [(shown[c.id], c.match_reason) for c in cs if c and 0 <= c.id < len(shown)]

    model_best = next((p for p, _ in known([picks.best_match])), None) if picks else None
    trusted = model_best is not None and model_best.tier != "used"
    if trusted:
        best = priced(model_best, "new", reason(picks.best_match.match_reason, "Closest match"), None)
    elif new:
        top = new[0]
        best = priced(top, "new", "Closest visual match" if top.match_score is not None
                      else "Top search result", None)
    else:
        best = None

    # Found means a match score of FOUND_SCORE or more (unscored products can't be checked, so they pass).
    # Below it, the highest-scoring product is the match if it clears the bar, else it's only the closest.
    exact = best is None or best.match_score is None or best.match_score >= FOUND_SCORE
    if not exact:
        top = max((p for p in new if p.match_score is not None), key=lambda p: p.match_score)
        exact = top.match_score >= FOUND_SCORE
        best = priced(top, "new", "Highest match score" if exact else NOT_EXACT_REASON, None)
    if not exact:  # no new listing is the item, but a used or refurbished one may be: that's still found
        found_used = max((p for p in products if p.tier == "used" and (p.match_score or 0) >= FOUND_SCORE),
                         key=lambda p: p.match_score, default=None)
        if found_used:
            exact = True
            best = priced(found_used, "used", USED_MATCH_REASON, None)

    seen = {best.url} if best else set()

    def take(pairs: list[tuple[Product, str]], tier: str, limit: int) -> list[Product]:
        """Up to `limit` (product, reason) pairs, skipping anything already in another list."""
        out = []
        for p, why in pairs:
            if p.url not in seen and len(out) < limit:
                seen.add(p.url)
                out.append(priced(p, tier, why, best))
        return out

    def cheaper(p: Product) -> bool:
        return best is not None and p.tier == "new" and p.price < best.price

    if picks is not None:
        dupes = take([(p, reason(r, DUPE_REASON)) for p, r in known(picks.dupes) if cheaper(p)],
                     "dupe", MAX_DUPES)
        used = take([(p, reason(r, USED_REASON)) for p, r in known(picks.used) if p.tier == "used"],
                    "used", MAX_USED)
    else:
        lookalikes = sorted((p for p in new if cheaper(p) and (p.match_score or 0) >= FALLBACK_DUPE_SCORE),
                            key=lambda p: p.price)
        used_all = sorted((p for p in products if p.tier == "used"),
                          key=lambda p: (p.match_score is None, -(p.match_score or 0)))
        dupes = take([(p, DUPE_REASON) for p in lookalikes], "dupe", MAX_DUPES)
        used = take([(p, USED_REASON) for p in used_all], "used", MAX_USED)

    final = [p for p in [best, *dupes, *used] if p]
    # Claude's summary describes its own pick, so it's only kept when that pick is the exact match.
    from_model = trusted and exact and best.url == model_best.url and summary_ok(picks.spoken_summary, final)
    if picks and not from_model:
        log.info("rank: replaced Claude's summary %r", picks.spoken_summary)
    if from_model:
        summary = picks.spoken_summary.strip()
    else:
        summary = template_summary(item, best, dupes, used) if exact else closest_summary(item, best)
    v = verdict(best, insight) if exact else "fair"
    text = verdict_text(best, dupes, used, insight, v) if exact else closest_text(insight)
    log.info("rank: picks from %s, summary from %s: best=%s (%s) dupes=%d used=%d verdict=%s",
             "claude" if picks else "fallback", "claude" if from_model else "template",
             best.store if best else None, "exact" if exact else "closest", len(dupes), len(used), v)
    return SearchResponse(
        capture_id="", best_match=best, dupes=dupes, used=used, spoken_summary=summary, timing_ms={},
        price_insight=insight.model_copy(update={"best_match_verdict": v, "verdict_text": text}),
    )


async def choose(item: Item, products: list[Product], insight: PriceInsight) -> SearchResponse:
    """Best match, dupes and used picks, verdict filled in. main.py sets capture_id and timing_ms."""
    if not products:
        return SearchResponse(capture_id="", best_match=None, dupes=[], used=[], price_insight=insight,
                              spoken_summary=NOT_FOUND, timing_ms={})
    resp = build(item, products, insight, await ask(item, shortlist(products), insight))
    resp.best_match = await search.with_store_link(resp.best_match)  # found: link to the store, not Google
    return resp


async def main(item_path: str, products_path: str):
    item = Item.model_validate_json(Path(item_path).read_text())
    products = [Product.model_validate(p) for p in json.loads(Path(products_path).read_text())]
    t0 = time.perf_counter()
    resp = await choose(item, products, pricing.compute_price_insight(products))
    print(resp.model_dump_json(indent=2))
    print(f"choose: {ms_since(t0)}ms total (Claude latency is in the log line above)", file=sys.stderr)
    await client().close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
