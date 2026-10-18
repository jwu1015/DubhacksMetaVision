"""W4: where to buy it. Google Shopping and Amazon through SerpAPI, searched with Claude's queries.

    python search.py <public_image_url> fixtures/item_lamp.json [--out products.json]

Per item: Shopping for the item's first 2 search queries (Claude writes them in vision.py, most specific
first; the SAM label when Claude didn't describe the item) and Amazon for the first, all at once. Then
match.py has Claude rate how likely each result's photo is the same product (match_score). Once rank has
chosen, with_store_link() swaps a found best match's Google Shopping page for its direct store link (one more
search). search_room does up to 5 items at once.

Lens isn't called: through SerpAPI it took 14-45s per photo (Oct 17) and often found nothing, while
Claude's queries named the product in 2-3s. eBay isn't called either: Shopping already lists eBay sellers and
second-hand offers. lens_raw, parse_lens, ebay_search and parse_ebay stay for reference.

Field names were checked against saved SerpAPI responses (tests/data/) and, for Lens
visual_matches, the example in SerpAPI's Google Lens docs.

SERP_DEV_CACHE=true replays raw responses from .serp_dev_cache/ instead of spending searches while
iterating. Keep it off for the demo.
"""
import argparse
import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import config
import match
import net
from models import Item, Product

log = logging.getLogger("gtl.search")
# httpx logs every request URL at INFO, and SerpAPI takes api_key as a URL parameter.
logging.getLogger("httpx").setLevel(logging.WARNING)

SERPAPI_URL = "https://serpapi.com/search.json"
# Per-source limits. 5s caps how long one slow search can hold up an item: fresh calls took 0.7-2.6s
# (Shopping, Amazon), but about 1 in 12 hung for the whole limit. (Lens took 17-64s; it isn't called.)
TIMEOUT_S = {"lens": 15.0, "shopping": 5.0, "ebay": 5.0, "amazon": 5.0}
# "all" (the default) returned only an ai_overview link and no visual_matches in our one test, so ask
# for visual_matches directly. Compare again once Lens returns results.
LENS_TYPE = "visual_matches"
DEV_CACHE = config.ROOT / ".serp_dev_cache"

TRACKING_PARAMS = {"gclid", "srsltid", "fbclid"}  # plus anything starting with utm_
USED = re.compile(r"pre-?owned|\bused\b|refurb|renewed", re.I)  # Amazon "Renewed" is refurbished
NON_USD = re.compile(
    r"[€£¥₹₩₽₺₪₫₱฿]"
    r"|\b(?:EUR|GBP|JPY|CAD|AUD|NZD|INR|CHF|MXN|BRL|CNY)\b"
    r"|\b(?!US)[A-Z]{1,3}\$"  # CA$, A$, R$... but not US$
)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Per-process tallies, so the logs show how often each source times out across test images.
calls: Counter = Counter()
timeouts: Counter = Counter()


class SerpError(Exception):
    pass


# ---------- normalizing ----------

def parse_price(extracted=None, raw: str | None = None, currency: str | None = None) -> float | None:
    """USD price as a float, or None when missing, free, or not USD.

    Prefers SerpAPI's pre-parsed number; otherwise reads the first number in the string, so a
    range like "$80 – $120" gives its low end.
    """
    if currency and currency.strip().upper() not in ("$", "USD", "US$"):
        return None
    if raw and NON_USD.search(raw):
        return None
    if isinstance(extracted, (int, float)) and not isinstance(extracted, bool):
        return float(extracted) if extracted > 0 else None
    m = NUMBER.search(raw or "")
    value = float(m.group().replace(",", "")) if m else 0.0
    return value if value > 0 else None


def normalize_url(url: str) -> str:
    """Key for same-URL dedupe: lowercase host, no fragment, no tracking params.

    google.com URLs keep their whole query string: on redirect and product links it is what
    identifies the product.
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    query = parts.query
    if not (host == "google.com" or host.endswith(".google.com")):
        query = urlencode([
            (k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)
        ])
    return urlunsplit((parts.scheme.lower(), host, parts.path, query, ""))


def _title_key(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", title.lower()).split())


def dedupe(products: list[Product]) -> list[Product]:
    """Drop repeats, keeping the first. Pass products in lens -> shopping -> ebay order.

    Repeats are the same URL (see normalize_url), or the same store with a near-identical title.
    """
    seen_urls: set[str] = set()
    kept: list[tuple[Product, str, str]] = []  # (product, store key, title key)
    for p in products:
        url = normalize_url(p.url)
        store, title = p.store.strip().lower(), _title_key(p.title)
        if url in seen_urls or any(
            s == store and difflib.SequenceMatcher(None, t, title).ratio() >= 0.92 for _, s, t in kept
        ):
            continue
        seen_urls.add(url)
        kept.append((p, store, title))
    return [p for p, _, _ in kept]


# ---------- parsers: raw SerpAPI JSON -> (priced products, number dropped for no price) ----------

def parse_lens(raw: dict) -> tuple[list[Product], int]:
    out, dropped = [], 0
    for rank, r in enumerate(raw.get("visual_matches") or []):
        if not (r.get("link") and r.get("title")):
            continue
        p = r.get("price") if isinstance(r.get("price"), dict) else {}
        price = parse_price(p.get("extracted_value"), p.get("value"), p.get("currency"))
        if price is None:
            dropped += 1
            continue
        tier = "used" if USED.search(r.get("condition") or "") else "new"  # live results: "Used" on vintage
        out.append(Product(title=r["title"], store=r.get("source") or "", price=price, url=r["link"],
                           image_url=r.get("thumbnail"), source="lens", tier=tier, source_rank=rank))
    return out, dropped


def parse_shopping(raw: dict, rank_offset: int = 0) -> tuple[list[Product], int]:
    out, dropped = [], 0
    for i, r in enumerate(raw.get("shopping_results") or []):
        url = r.get("link") or r.get("product_link")  # live results only had product_link
        if not (url and r.get("title")):
            continue
        price = parse_price(r.get("extracted_price"), r.get("price"))
        if price is None:
            dropped += 1
            continue
        tier = "used" if r.get("second_hand_condition") else "new"  # "pre-owned" or "refurbished"
        out.append(Product(title=r["title"], store=r.get("source") or "", price=price, url=url,
                           image_url=r.get("thumbnail"), source="shopping", tier=tier, source_rank=rank_offset + i))
    return out, dropped


def parse_ebay(raw: dict) -> tuple[list[Product], int]:
    out, dropped = [], 0
    for rank, r in enumerate(raw.get("organic_results") or []):
        if not (r.get("link") and r.get("title")):
            continue
        p = r.get("price") if isinstance(r.get("price"), dict) else {}
        p = p.get("from", p)  # ranges come as {"from": {raw, extracted}, "to": {...}}
        price = parse_price(p.get("extracted"), p.get("raw"))
        if price is None:
            dropped += 1
            continue
        tier = "used" if USED.search(r.get("condition") or "") else "new"
        out.append(Product(title=r["title"], store="eBay", price=price, url=r["link"],
                           image_url=r.get("thumbnail"), source="ebay", tier=tier, source_rank=rank))
    return out, dropped


# ---------- SerpAPI ----------


def parse_amazon(raw: dict) -> tuple[list[Product], int]:
    out, dropped = [], 0
    for rank, r in enumerate(raw.get("organic_results") or []):
        url = r.get("link_clean") or r.get("link")  # link_clean skips the ad redirect on sponsored results
        if not (url and r.get("title")):
            continue
        price = parse_price(r.get("extracted_price"), r.get("price"))
        if price is None:
            dropped += 1
            continue
        tier = "used" if USED.search(r["title"]) else "new"
        out.append(Product(title=r["title"], store="Amazon", price=price, url=url,
                           image_url=r.get("thumbnail"), source="amazon", tier=tier, source_rank=rank))
    return out, dropped

def _dev_cache_on() -> bool:
    return os.getenv("SERP_DEV_CACHE", "false").strip().lower() in ("1", "true", "yes")


def log_serpapi_time(name: str | None, data: dict, waited: float):
    """SerpAPI's own processing time next to our wait for the request: a big gap means the delay is ours."""
    took = (data.get("search_metadata") or {}).get("total_time_taken")
    if not isinstance(took, (int, float)):
        log.info("search %s: our request %.1fs (SerpAPI gave no processing time)", name, waited)
    elif waited < took:  # can't get results faster than they were made: SerpAPI replayed its 1-hour cache
        log.info("search %s: our request %.1fs, served from SerpAPI's cache (first run took %.1fs)",
                 name, waited, took)
    else:
        log.info("search %s: SerpAPI processing %.1fs, our request %.1fs", name, took, waited)


async def serp(params: dict, capture_id: str | None = None, name: str | None = None) -> dict:
    """One SerpAPI search -> raw JSON, also written to logs/<capture_id>/<name>.json.

    Raises on HTTP errors; the caller turns any exception into [] for that source.
    """
    cache = DEV_CACHE / (params["engine"] + "_"
                         + hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16] + ".json")
    if _dev_cache_on() and cache.exists():
        data = json.loads(cache.read_text())
    else:
        t0 = time.perf_counter()
        r = await net.client().get(SERPAPI_URL, params=params | {"api_key": os.getenv("SERPAPI_KEY", "")},
                                   timeout=max(TIMEOUT_S.values()))  # _run's wait_for is the real limit
        data = r.json()  # SerpAPI errors are JSON too
        log_serpapi_time(name, data, time.perf_counter() - t0)
        if r.status_code != 200:
            raise SerpError(f"HTTP {r.status_code}: {data.get('error')}")
        if _dev_cache_on():
            DEV_CACHE.mkdir(exist_ok=True)
            cache.write_text(json.dumps(data))
    if capture_id and name:
        folder = config.LOGS / capture_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(data, indent=2))
    if data.get("error"):  # e.g. "Google Lens hasn't returned any results for this query."
        log.info("search %s: SerpAPI says %r", name, data["error"])
    return data


async def lens_raw(cutout_url: str, capture_id: str | None = None) -> dict:
    return await serp({"engine": "google_lens", "url": cutout_url, "type": LENS_TYPE, "hl": "en", "country": "us"},
                      capture_id, "lens")


# Google Shopping results carry no store link, only a token for the product's page of sellers. Kept by result
# URL, so the found product's direct link can be fetched later (one more search, found items only).
PAGE_TOKENS: dict[str, str] = {}
STORE_LINK_TIMEOUT_S = 5.0


async def shopping_search(query: str, capture_id: str | None = None,
                          name: str = "shopping_0") -> tuple[list[Product], int]:
    raw = await serp({"engine": "google_shopping", "q": query, "gl": "us", "hl": "en"}, capture_id, name)
    if len(PAGE_TOKENS) > 5000:  # a long-running server: forget old searches
        PAGE_TOKENS.clear()
    for r in raw.get("shopping_results") or []:
        url, token = r.get("link") or r.get("product_link"), r.get("immersive_product_page_token")
        if url and token:
            PAGE_TOKENS[url] = token
    return parse_shopping(raw)


async def store_link(p: Product) -> str | None:
    """A Shopping result's direct link at its store, from Google's page of sellers; None if there isn't one."""
    token = PAGE_TOKENS.get(p.url) if p.source == "shopping" else None
    if not token:
        return None
    raw = await serp({"engine": "google_immersive_product", "page_token": token})
    stores = [s for s in (raw.get("product_results") or {}).get("stores") or [] if s.get("link")]
    same = next((s for s in stores if (s.get("name") or "").lower() == p.store.lower()), None)
    return (same or (stores[0] if stores else {})).get("link")


async def ebay_search(query: str, capture_id: str | None = None) -> tuple[list[Product], int]:
    raw = await serp({"engine": "ebay", "_nkw": query, "ebay_domain": "ebay.com"}, capture_id, "ebay")
    return parse_ebay(raw)


async def amazon_search(query: str, capture_id: str | None = None) -> tuple[list[Product], int]:
    raw = await serp({"engine": "amazon", "k": query, "amazon_domain": "amazon.com"}, capture_id, "amazon")
    return parse_amazon(raw)


# ---------- search_all ----------

def _scrub(e: Exception) -> str:
    key = os.getenv("SERPAPI_KEY")
    text = f"{type(e).__name__}: {e}"
    return text.replace(key, "***") if key else text


async def _run(name: str, job, stats: dict) -> list[Product]:
    """Run one source with its timeout. Never raises: failures and timeouts give []."""
    source = name.split("_")[0]
    calls[source] += 1
    t0 = time.perf_counter()
    status, products, dropped = "ok", [], 0
    try:
        products, dropped = await asyncio.wait_for(job, TIMEOUT_S[source])
    except TimeoutError:
        timeouts[source] += 1
        status = f"timeout after {TIMEOUT_S[source]}s"
    except Exception as e:
        status = f"error ({_scrub(e)})"
    ms = round((time.perf_counter() - t0) * 1000)
    stats[name] = {"status": status, "priced": len(products), "dropped_no_price": dropped, "ms": ms}
    (log.info if status == "ok" else log.warning)(
        "search %s: %s, %d priced, %d dropped for no price, %dms (%s timeouts so far: %d of %d)",
        name, status, len(products), dropped, ms, source, timeouts[source], calls[source])
    return products


# ---------- search_all / search_room ----------

MAX_ROOM_ITEMS = 5
ROOM_CONCURRENCY = 3  # items searched at once; each is about 3 SerpAPI calls and one Claude call


def queries_for(item: Item) -> list[str]:
    """Claude's search queries, most specific first; the SAM label (category) when there are none."""
    return [q.strip() for q in item.search_queries if q.strip()] or ([item.category] if item.category else [])


async def search_all_with_stats(cutout_url: str, item: Item,
                                capture_id: str | None = None) -> tuple[list[Product], dict]:
    queries = queries_for(item)
    jobs = {}
    for i, q in enumerate(queries[:2]):
        jobs[f"shopping_{i}"] = shopping_search(q, capture_id, f"shopping_{i}")
    if queries:
        jobs["amazon"] = amazon_search(queries[0], capture_id)  # the first query is the most specific

    stats: dict = dict.fromkeys(jobs)  # filled by _run; keeps job order for printing
    results = dict(zip(jobs, await asyncio.gather(*(_run(n, j, stats) for n, j in jobs.items()))))

    # Shopping query 1 continues query 0's numbering.
    offset = max((p.source_rank for p in results.get("shopping_0", [])), default=-1) + 1
    for p in results.get("shopping_1", []):
        p.source_rank += offset

    products = dedupe([p for name in jobs for p in results[name]])
    products = await match.score(cutout_url, item, products)
    log.info("search %s: queries=%s, %d products after dedupe", capture_id, queries, len(products))
    return products, stats


async def search_all(cutout_url: str, item: Item, capture_id: str | None = None) -> list[Product]:
    """Shopping for the item's first 2 queries + Amazon for the first, at once, then a match score for each result.

    Normalized, priced, deduped. A failing or slow source contributes [] and never raises.
    """
    products, _ = await search_all_with_stats(cutout_url, item, capture_id)
    return products


async def search_room(items: list[tuple[Item, str]], capture_id: str | None = None,
                      on_result=None) -> list[list[Product]]:
    """Search up to 5 (item, crop_url) pairs at once; results line up with the input order.

    on_result(index, item, products) is awaited as each item finishes, so the app can show prices as they
    arrive. One failing item gives [] and does not affect the others.
    """
    sem = asyncio.Semaphore(ROOM_CONCURRENCY)

    async def one(i: int, item: Item, url: str) -> list[Product]:
        try:
            async with sem:
                products = await search_all(url, item, f"{capture_id}_{i}" if capture_id else None)
        except Exception as e:
            log.warning("search %s item %d (%s) failed: %s", capture_id, i, item.category, _scrub(e))
            products = []
        if on_result:
            await on_result(i, item, products)
        return products

    return list(await asyncio.gather(*(one(i, item, url) for i, (item, url) in enumerate(items[:MAX_ROOM_ITEMS]))))


async def with_store_link(best: Product | None) -> Product | None:
    """The chosen best match, if found (match score FOUND_SCORE or more), with its direct store link in place
    of Google Shopping's page: one more search. eBay results already link to the listing. Never raises."""
    if best is None or (best.match_score or 0) < match.FOUND_SCORE:
        return best
    try:
        link = await asyncio.wait_for(store_link(best), STORE_LINK_TIMEOUT_S)
    except Exception as e:  # timeout, SerpAPI error: keep the link it has
        log.warning("search: no store link for %r: %s", best.title[:60], _scrub(e))
        return best
    if link:
        log.info("search: found %r (score %s), store link %s", best.title[:60], best.match_score, link)
    return best.model_copy(update={"url": link}) if link else best


# ---------- CLI ----------

async def _cli(image_url: str, item_path: str, out: str | None):
    item = Item.model_validate_json(Path(item_path).read_text())
    capture_id = "cli_" + time.strftime("%Y%m%d_%H%M%S")
    t0 = time.perf_counter()
    try:
        products, stats = await search_all_with_stats(image_url, item, capture_id)
    finally:
        await net.close()
    total_ms = round((time.perf_counter() - t0) * 1000)

    print(f"\nqueries: {queries_for(item)}")
    print(f"{'source':12} {'priced':>6} {'no price':>8} {'ms':>6}  status")
    for name, s in stats.items():
        print(f"{name:12} {s['priced']:>6} {s['dropped_no_price']:>8} {s['ms']:>6}  {s['status']}")
    print(f"\n{len(products)} products after dedupe, total {total_ms} ms (raw responses in logs/{capture_id}/)\n")
    for p in sorted(products, key=lambda p: (p.match_score is None, -(p.match_score or 0)))[:10]:
        score = "-" if p.match_score is None else p.match_score
        print(f"{score:>3}  {p.source:8} #{p.source_rank:<3} {p.tier:4} ${p.price:>9,.2f}  "
              f"{p.store[:18]:18}  {p.title[:60]}")
    if out:
        Path(out).write_text(json.dumps([p.model_dump() for p in products], indent=2) + "\n")
        print(f"\nwrote {len(products)} products to {out}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Run search_all for one image URL + Item JSON.")
    ap.add_argument("image_url", help="public URL of the crop (or any product photo)")
    ap.add_argument("item_json", help="Item JSON file, e.g. fixtures/item_lamp.json")
    ap.add_argument("--out", help="also write the products as JSON here")
    args = ap.parse_args()
    asyncio.run(_cli(args.image_url, args.item_json, args.out))
