"""W4: where to buy it. Google Lens on the cutout + Google Shopping + eBay, all through SerpAPI.

    python search.py <public_image_url> fixtures/item_lamp.json [--out products.json]

Field names were checked against saved SerpAPI responses (tests/data/) and, for Lens
visual_matches, the example in SerpAPI's Google Lens docs (live Lens calls failed while this was
written, so re-check parse_lens against a real response once Lens works).

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
import net
from models import Item, Product

log = logging.getLogger("gtl.search")
# httpx logs every request URL at INFO, and SerpAPI takes api_key as a URL parameter.
logging.getLogger("httpx").setLevel(logging.WARNING)

SERPAPI_URL = "https://serpapi.com/search.json"
# Per-source budget so search_all stays under ~3.5s. SerpAPI's published 30-day average for Lens
# is ~6.3s, so expect Lens timeouts; raising "lens" needs A1 to accept a longer search budget.
TIMEOUT_S = {"lens": 3.2, "shopping": 3.2, "ebay": 3.2}
# "all" (the default) returned only an ai_overview link and no visual_matches in our one test, so ask
# for visual_matches directly. Compare again once Lens returns results.
LENS_TYPE = "visual_matches"
DEV_CACHE = config.ROOT / ".serp_dev_cache"

TRACKING_PARAMS = {"gclid", "srsltid", "fbclid"}  # plus anything starting with utm_
USED = re.compile(r"pre-?owned|\bused\b|refurb", re.I)
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
        out.append(Product(title=r["title"], store=r.get("source") or "", price=price, url=url,
                           image_url=r.get("thumbnail"), source="shopping", source_rank=rank_offset + i))
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

def _dev_cache_on() -> bool:
    return os.getenv("SERP_DEV_CACHE", "false").strip().lower() in ("1", "true", "yes")


async def serp(params: dict, capture_id: str | None = None, name: str | None = None) -> dict:
    """One SerpAPI search -> raw JSON, also written to logs/<capture_id>/<name>.json.

    Raises on HTTP errors; the caller turns any exception into [] for that source.
    """
    cache = DEV_CACHE / (params["engine"] + "_"
                         + hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16] + ".json")
    if _dev_cache_on() and cache.exists():
        data = json.loads(cache.read_text())
    else:
        r = await net.client().get(SERPAPI_URL, params=params | {"api_key": os.getenv("SERPAPI_KEY", "")},
                                   timeout=max(TIMEOUT_S.values()))  # _run's wait_for is the real limit
        data = r.json()  # SerpAPI errors are JSON too
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


async def lens_search(cutout_url: str, capture_id: str | None = None) -> tuple[list[Product], int]:
    raw = await serp({"engine": "google_lens", "url": cutout_url, "type": LENS_TYPE, "hl": "en", "country": "us"},
                     capture_id, "lens")
    return parse_lens(raw)


async def shopping_search(query: str, capture_id: str | None = None,
                          name: str = "shopping_0") -> tuple[list[Product], int]:
    raw = await serp({"engine": "google_shopping", "q": query, "gl": "us", "hl": "en"}, capture_id, name)
    return parse_shopping(raw)


async def ebay_search(query: str, capture_id: str | None = None) -> tuple[list[Product], int]:
    raw = await serp({"engine": "ebay", "_nkw": query, "ebay_domain": "ebay.com"}, capture_id, "ebay")
    return parse_ebay(raw)


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


async def search_all_with_stats(cutout_url: str, item: Item,
                                capture_id: str | None = None) -> tuple[list[Product], dict]:
    queries = item.search_queries or [q for q in (item.description, item.category) if q]
    jobs = {"lens": lens_search(cutout_url, capture_id)}
    for i, q in enumerate(queries[:2]):
        jobs[f"shopping_{i}"] = shopping_search(q, capture_id, f"shopping_{i}")
    if queries:
        jobs["ebay"] = ebay_search(queries[0], capture_id)  # first query is the most specific

    stats: dict = dict.fromkeys(jobs)  # filled by _run; keeps job order for printing
    results = dict(zip(jobs, await asyncio.gather(*(_run(n, j, stats) for n, j in jobs.items()))))

    # Shopping query 1 continues query 0's numbering.
    offset = max((p.source_rank for p in results.get("shopping_0", [])), default=-1) + 1
    for p in results.get("shopping_1", []):
        p.source_rank += offset

    products = dedupe([p for name in jobs for p in results[name]])
    log.info("search %s: %d products after dedupe", capture_id, len(products))
    return products, stats


async def search_all(cutout_url: str, item: Item, capture_id: str | None = None) -> list[Product]:
    """Lens on the cutout + Shopping for the first 2 queries + eBay, concurrently.

    Normalized, priced, deduped. A failing or slow source contributes [] and never raises.
    """
    products, _ = await search_all_with_stats(cutout_url, item, capture_id)
    return products


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

    print(f"\n{'source':12} {'priced':>6} {'no price':>8} {'ms':>6}  status")
    for name, s in stats.items():
        print(f"{name:12} {s['priced']:>6} {s['dropped_no_price']:>8} {s['ms']:>6}  {s['status']}")
    print(f"\n{len(products)} products after dedupe, total {total_ms} ms (raw responses in logs/{capture_id}/)\n")
    for p in products[:10]:
        print(f"{p.source:8} #{p.source_rank:<3} {p.tier:4} ${p.price:>9,.2f}  {p.store[:18]:18}  {p.title[:60]}")
    if out:
        Path(out).write_text(json.dumps([p.model_dump() for p in products], indent=2) + "\n")
        print(f"\nwrote {len(products)} products to {out}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Run search_all for one image URL + Item JSON.")
    ap.add_argument("image_url", help="public URL of the cutout (or any product photo)")
    ap.add_argument("item_json", help="Item JSON file, e.g. fixtures/item_lamp.json")
    ap.add_argument("--out", help="also write the products as JSON here")
    args = ap.parse_args()
    asyncio.run(_cli(args.image_url, args.item_json, args.out))
