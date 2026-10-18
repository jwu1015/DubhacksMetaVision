"""W1: Claude looks at the photo and returns the Item (category, description, queries, rough bbox).

    python vision.py fixtures/lamp_room.jpg      # prints the Item, writes fixtures/lamp_room_box.jpg
"""
import asyncio
import base64
import io
import logging
import sys
import time
from pathlib import Path

import anthropic
from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ValidationError

import config  # noqa: F401 - loads .env so the client finds ANTHROPIC_API_KEY
from models import BBox, Item

log = logging.getLogger("gtl.vision")

MODEL = "claude-sonnet-5-5"
MAX_EDGE = 1024  # resize before the call; never upscale

SYSTEM_PROMPT = """\
You identify consumer products in photos for a shopping app.
The photo was taken from smart glasses, so the item the user is looking at
is usually the most prominent object near the center of the frame. Focus on
that single item. Ignore people, walls, floors, ceilings, and windows.

Fill in these fields:
- category: short noun phrase (e.g. "floor lamp", "running shoes", "backpack", "coffee maker")
  - this is also used as a segmentation text prompt, so keep it simple
- description: one detailed sentence a shopper would type into a search box,
  including shape, color, material, and distinctive features
- style: design style if the product has one (e.g. "mid-century modern", "minimalist",
  "streetwear", "industrial"), else null
- colors: 1-3 colors
- materials: 1-3 materials
- brand_guess: brand if there is a visible logo or very distinctive design, else null
- confidence: 0-1, how sure you are of the category and description
- search_queries: 3 shopping search queries, most specific to most generic
- bbox: bounding box around that item as normalized coordinates
  {"x": left, "y": top, "w": width, "h": height}, each between 0 and 1,
  relative to the full image
- quick_phrase: under 8 words, natural speech, e.g.
  "Mid-century floor lamp. Finding prices."
If no shoppable item is visible, set category to null and leave the other
fields empty or null."""


# The structured output Claude must return; to_item() maps it onto models.Item. (No docstring:
# Pydantic would send it to the model as the schema description.)
class VisionOut(BaseModel):
    category: str | None
    description: str | None
    style: str | None
    colors: list[str]
    materials: list[str]
    brand_guess: str | None
    confidence: float
    search_queries: list[str]
    bbox: BBox | None
    quick_phrase: str | None


_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        # /detect has a ~2s budget; the SDK default (10 min, 2 retries) would hang the app.
        _client = anthropic.AsyncAnthropic(timeout=15.0, max_retries=1)
    return _client


def prepare(image_bytes: bytes) -> tuple[str, tuple[int, int]]:
    """JPEG bytes -> base64 of a copy at most MAX_EDGE on the long side, plus that copy's size."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode(), img.size


def clean_bbox(box: BBox | None, size: tuple[int, int]) -> BBox | None:
    """Clamp to the image; accept pixel coordinates by mistake; None if nothing is left."""
    if box is None:
        return None
    x, y, w, h = box.x, box.y, box.w, box.h
    if max(x, y, w, h) > 1.5:  # the model answered in pixels of the image it saw
        x, w, y, h = x / size[0], w / size[0], y / size[1], h / size[1]
    x0, y0 = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
    x1, y1 = min(max(x + w, 0.0), 1.0), min(max(y + h, 0.0), 1.0)
    if x1 - x0 <= 0.01 or y1 - y0 <= 0.01:
        return None
    return BBox(x=round(x0, 4), y=round(y0, 4), w=round(x1 - x0, 4), h=round(y1 - y0, 4))


def to_item(out: VisionOut, size: tuple[int, int]) -> Item:
    category = (out.category or "").strip()
    if not category or category.lower() in ("null", "none"):
        return Item(category=None)
    phrase = (out.quick_phrase or "").strip()
    if not phrase or len(phrase.split()) >= 8:
        phrase = f"{category[0].upper()}{category[1:]}. Finding prices."
    queries = [q.strip() for q in out.search_queries if q.strip()][:3]
    brand = (out.brand_guess or "").strip()
    return Item(
        category=category,
        description=out.description,
        style=out.style,
        colors=out.colors[:3],
        materials=out.materials[:3],
        brand_guess=brand if brand.lower() not in ("", "null", "none", "unknown") else None,
        confidence=min(max(out.confidence, 0.0), 1.0),
        search_queries=queries or [out.description or category],
        bbox=clean_bbox(out.bbox, size),
        quick_phrase=phrase,
    )


async def detect(image_bytes: bytes) -> Item:
    """Identify the main item. Unusable output twice -> Item(category=None); API errors raise."""
    b64, size = prepare(image_bytes)
    for attempt in (1, 2):
        t0 = time.perf_counter()
        try:
            resp = await client().beta.messages.parse(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": "What is the main item in this photo?"},
                ]}],
                output_format=VisionOut,
                output_config={"effort": "low"},  # extraction: low effort skips thinking on most photos
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (ValidationError, ValueError) as e:  # output that doesn't fit VisionOut
            log.warning("vision attempt %d: unparseable output (%s)", attempt, str(e).splitlines()[0])
            continue
        ms = round((time.perf_counter() - t0) * 1000)
        if resp.stop_reason == "refusal":
            log.warning("vision refused (%s) in %dms", resp.stop_details, ms)
            return Item(category=None)
        if resp.parsed_output is None:
            log.warning("vision attempt %d: no output (stop_reason=%s)", attempt, resp.stop_reason)
            continue
        log.info("vision %dms attempt=%d tokens in=%d out=%d", ms, attempt,
                 resp.usage.input_tokens, resp.usage.output_tokens)
        return to_item(resp.parsed_output, size)
    return Item(category=None)


async def _cli(path: str):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = Path(path).read_bytes()
    t0 = time.perf_counter()
    item = await detect(data)
    print(item.model_dump_json(indent=2))
    print(f"{round((time.perf_counter() - t0) * 1000)}ms")
    if item.bbox:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
        W, H = img.size
        b = item.bbox
        ImageDraw.Draw(img).rectangle(
            [b.x * W, b.y * H, (b.x + b.w) * W, (b.y + b.h) * H], outline=(255, 60, 60), width=max(3, W // 200))
        out = Path(path).with_name(Path(path).stem + "_box.jpg")
        img.save(out, quality=85)
        print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1]))
