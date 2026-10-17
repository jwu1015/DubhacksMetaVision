"""W3: re-rank search results by CLIP similarity between the cutout and each thumbnail.

    python rerank.py <cutout_url> fixtures/products_raw_lamp.json [--fixture]

--fixture answers /embed from fixtures/embed_service_*.json instead of calling a service.
rerank() never raises: if /embed fails in any way, products come back in input order, unscored.
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

import config
import net
from models import Product

log = logging.getLogger("gtl")

EMBED_URL = (os.getenv("EMBED_SERVICE_URL") or config.MODEL_SERVICE_URL).rstrip("/")
TIMEOUT_S = 3.0
MAX_CANDIDATES = 25
LOW, HIGH = 0.55, 0.92  # raw similarity that maps to score 0 and 100; tune on real results
MIN_SCORE = 40


class EmbedResponse(BaseModel):
    similarities: list[float | None]
    latency_ms: int | None = None


def score(sim: float) -> int:
    return round(min(max((sim - LOW) / (HIGH - LOW), 0.0), 1.0) * 100)


def unscored(products: list[Product]) -> list[Product]:
    return [p.model_copy(update={"match_score": None}) for p in products]


async def embed(cutout_url: str, urls: list[str], client: httpx.AsyncClient) -> EmbedResponse:
    """POST /embed; raises on anything other than a well-formed answer for every URL."""
    if not EMBED_URL:
        raise RuntimeError("EMBED_SERVICE_URL and MODEL_SERVICE_URL are both unset")
    token = config.MODEL_SERVICE_TOKEN  # an empty "Bearer " header is illegal, so skip it when unset
    async with asyncio.timeout(TIMEOUT_S):
        r = await client.post(
            f"{EMBED_URL}/embed", timeout=TIMEOUT_S,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            json={"query_url": cutout_url, "candidate_urls": urls},
        )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    body = EmbedResponse.model_validate_json(r.content)
    if len(body.similarities) != len(urls):
        raise ValueError(f"{len(body.similarities)} similarities for {len(urls)} candidates")
    return body


async def rerank(cutout_url: str, products: list[Product],
                 client: httpx.AsyncClient | None = None) -> list[Product]:
    """Scored products best first (dropping those under MIN_SCORE), then unscored ones in input order."""
    t0 = time.perf_counter()
    with_thumb = sorted((i for i, p in enumerate(products) if p.image_url),
                        key=lambda i: products[i].source_rank)
    candidates = sorted(with_thumb[:MAX_CANDIDATES])  # back to input order
    urls = list(dict.fromkeys(products[i].image_url for i in candidates))
    if not urls:
        return unscored(products)
    try:
        body = await embed(cutout_url, urls, client or net.client())
    except Exception as e:  # timeout, connection, status, JSON, shape: all degrade the same way
        log.warning("rerank: /embed failed after %dms (%s: %s); products left unscored",
                    round((time.perf_counter() - t0) * 1000), type(e).__name__, " ".join(str(e).split())[:200])
        return unscored(products)

    sims = dict(zip(urls, body.similarities))
    scores = {i: score(s) for i in candidates if (s := sims[products[i].image_url]) is not None}
    out = [p.model_copy(update={"match_score": scores.get(i)}) for i, p in enumerate(products)]
    ranked = sorted((p for p in out if p.match_score is not None), key=lambda p: -p.match_score)
    kept = [p for p in ranked if p.match_score >= MIN_SCORE]
    rest = [p for p in out if p.match_score is None]
    log.info("rerank: %dms (embed service %sms) scored=%d dropped=%d unscored=%d",
             round((time.perf_counter() - t0) * 1000), body.latency_ms, len(kept),
             len(ranked) - len(kept), len(rest))
    return kept + rest


def fixture_transport() -> httpx.MockTransport:
    """Answers /embed from the fixture, matching similarities to candidate URLs."""
    req = json.loads((config.FIXTURES / "embed_service_request.json").read_text())
    resp = json.loads((config.FIXTURES / "embed_service_response.json").read_text())
    sims = dict(zip(req["candidate_urls"], resp["similarities"]))

    def handler(request: httpx.Request) -> httpx.Response:
        urls = json.loads(request.content)["candidate_urls"]
        return httpx.Response(200, json={"similarities": [sims.get(u) for u in urls],
                                         "latency_ms": resp["latency_ms"]})
    return httpx.MockTransport(handler)


async def main(args: list[str]):
    global EMBED_URL
    cutout_url, path = args[0], args[1]
    products = [Product.model_validate(p) for p in json.loads(Path(path).read_text())]
    client = None
    if "--fixture" in args:
        EMBED_URL, client = "http://embed.fixture", httpx.AsyncClient(transport=fixture_transport())
    print(json.dumps([p.model_dump(mode="json") for p in await rerank(cutout_url, products, client)],
                     indent=2))
    await net.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1:]))
