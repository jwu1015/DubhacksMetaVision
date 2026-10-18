"""Match score: Claude looks at the user's crop and each search result's photo and rates, 0-100, how
likely the result is the same product. It stands in for CLIP (rerank.py) while no /embed service is
deployed, and it judges "same product" rather than "looks similar".

    score(cutout, item, products) -> the same products, with match_score set on the top candidates

cutout is the crop's URL, or the crop itself (a background Look keeps it in memory only).

Never raises: if the crop can't be read or Claude fails, products come back unscored.
"""
import asyncio
import base64
import io
import logging
import time

import anthropic
from PIL import Image, ImageOps
from pydantic import BaseModel

from storefront import config, net
from storefront.models import Item, Product

log = logging.getLogger("storefront.match")

MODEL = "claude-sonnet-5-5"
FOUND_SCORE = 75                  # on the scale below: the same model (maybe another color) or better
MAX_CANDIDATES = 16               # result photos Claude sees, best source ranks first (~8 each: Shopping, Amazon)
CROP_EDGE, THUMB_EDGE = 768, 256  # longest side sent; small thumbnails keep the call fast
FETCH_TIMEOUT_S = 3.0
TIMEOUT_S = 10.0

SYSTEM = """You compare product photos for a shopping app. Image 0 is the item a user is
looking at, cropped from a photo they took, so it may be at an angle, partly hidden or in
poor light. The listings after it are store results from a text search, each with its
title and photo. For each listing, rate 0-100 how likely it shows the SAME product as image 0:
- 90-100: the same product: same brand, model and color
- 75-89: the same model in a different color, size or minor variant
- 50-74: the same kind of item with a similar look, but a different product
- 0-49: a different kind of item, or a clearly different look
Judge by what you can see: shape, proportions, materials, colors, logos, text and details.
Titles are hints only: a listing whose photo doesn't match never scores above 74 because of
its title. Return one score per listing, by its number."""


class Score(BaseModel):
    id: int
    score: int


class Scores(BaseModel):
    scores: list[Score]


OUTPUT_FORMAT = {"type": "json_schema", "schema": anthropic.transform_schema(Scores)}

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=0, default_headers=config.ANTHROPIC_HEADERS)
    return _client


def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


def candidates(products: list[Product]) -> list[int]:
    """Indexes of the products Claude scores: those with a photo, lowest source rank first, so each
    source's top results get a look."""
    with_photo = sorted((i for i, p in enumerate(products) if p.image_url), key=lambda i: products[i].source_rank)
    return with_photo[:MAX_CANDIDATES]


async def fetch(url: str | bytes) -> bytes | None:
    """Our own uploads are read from disk (no round trip through the tunnel); anything else is downloaded.
    Bytes are an image already in memory."""
    if isinstance(url, bytes):
        return url
    prefix = config.PUBLIC_BASE_URL + "/"
    if url.startswith(prefix):
        path = (config.ROOT / url[len(prefix):].split("?")[0]).resolve()
        if path.is_relative_to(config.UPLOADS.resolve()) and path.is_file():
            return path.read_bytes()
    try:
        r = await net.client().get(url, timeout=FETCH_TIMEOUT_S)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def jpeg_b64(data: bytes, edge: int) -> str:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((edge, edge))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode()


def image(b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


async def call_model(content: list[dict]) -> str:
    """One Claude call; returns the reply text."""
    msg = await client().messages.create(
        model=MODEL, max_tokens=1000, system=SYSTEM, timeout=TIMEOUT_S,
        messages=[{"role": "user", "content": content}],
        thinking={"type": "between_tools"},  # how this model turns thinking off; "disabled" is refused
        output_config={"effort": "low", "format": OUTPUT_FORMAT},
    )
    if msg.stop_reason != "end_turn":
        raise ValueError(f"stop_reason={msg.stop_reason}")
    return "".join(b.text for b in msg.content if b.type == "text")


async def score(cutout: str | bytes, item: Item, products: list[Product]) -> list[Product]:
    t0 = time.perf_counter()
    picks = candidates(products)
    if not picks:
        return products
    crop, *photos = await asyncio.gather(fetch(cutout), *(fetch(products[i].image_url) for i in picks))
    try:
        crop_b64 = jpeg_b64(crop, CROP_EDGE) if crop else None
    except Exception:
        crop_b64 = None
    if crop_b64 is None:
        log.warning("match: couldn't read the crop %s; products left unscored",
                    cutout if isinstance(cutout, str) else f"({len(cutout)} bytes in memory)")
        return products

    shown = []  # (product index, photo), numbered from 1 in the order Claude sees them
    for i, data in zip(picks, photos):
        if not data:
            continue
        try:
            shown.append((i, jpeg_b64(data, THUMB_EDGE)))
        except Exception:  # not an image we can read: leave that product unscored
            continue
    if not shown:
        return products

    described = item.description or item.category or "unknown"
    content = [{"type": "text", "text": f"Image 0, the item the user is looking at. Described as: {described}"},
               image(crop_b64)]
    for n, (i, b64) in enumerate(shown, 1):
        content += [{"type": "text", "text": f"Listing {n}: {products[i].title} ({products[i].store})"}, image(b64)]
    try:
        scores = Scores.model_validate_json(await call_model(content)).scores
    except Exception as e:  # API error, timeout, bad JSON or schema
        log.warning("match: claude failed after %dms: %s: %s; products left unscored",
                    ms_since(t0), type(e).__name__, " ".join(str(e).split())[:200])
        return products

    by_listing = {s.id: min(max(s.score, 0), 100) for s in scores if 1 <= s.id <= len(shown)}
    out = list(products)
    for n, (i, _) in enumerate(shown, 1):
        if n in by_listing:
            out[i] = out[i].model_copy(update={"match_score": by_listing[n]})
    log.info("match: %dms, scored %d of %d shown (best %s)", ms_since(t0), len(by_listing), len(shown),
             max(by_listing.values(), default=None))
    return out
