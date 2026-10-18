"""Which objects SAM should look for: Claude picks them from a frame, within a category (moved here from
the segmentation server on 18 Oct; the server now only takes labels).

tracker.py asks before starting a session (the session starts once the labels are back), and segment.py
before a one-photo session, whenever SAM_CATEGORIES is empty.

    python labels.py fixtures/lamp_room.jpg "furniture and home decor"
"""
import asyncio
import base64
import io
import logging
import os
import sys
import time

import anthropic
from PIL import Image
from pydantic import BaseModel

import config  # loads .env before the Anthropic client reads ANTHROPIC_API_KEY

log = logging.getLogger("gtl.labels")

MODEL = "claude-haiku-5-5"
MAX_LABELS = int(os.getenv("MAX_LABELS", "12"))
TIMEOUT_S = 10.0  # the session waits on this, so fail fast; the category is the fallback
EDGE = 512        # enough to name objects, and a smaller upload answers faster

SYSTEM = f"""\
You pick text prompts for an open-vocabulary segmentation model (SAM 3) that will track objects
in a video from smart glasses. You see only the first frame, but the wearer will look around and
move through this place, so the labels must cover what they are likely to see next, not just now.

The user gives a category (e.g. "electronics", "kitchen utensils", "things to buy"). Every label
must be a kind of object in that category; leave out everything else in the scene, however prominent.

Return one label per kind of object (not per instance), up to {MAX_LABELS}, in two groups:
1. every kind of object in the category visible in the image, most prominent first
2. then objects in the category you would expect in this kind of place (an office, kitchen, living
   room, street, store...) that are not on screen yet: e.g. for "electronics", a desk with a monitor
   suggests keyboard, mouse, headphones, phone, cable
Give as many labels as the place plausibly holds, up to {MAX_LABELS}; return fewer only when the
category itself is narrow.

Every label:
- exactly one word: a very general, lowercase, singular noun ("lamp", "phone", "cable", "cup",
  "bottle"), never a compound or a description ("desk lamp", "cell phone", "phone charger",
  "water bottle", "blue mug")
- a concrete, physical object that can be outlined in an image; not terrain (snow, grass) or markings
- not a person or body part
- not a wall, floor, ceiling, window, door or other part of the building
- no duplicates or synonyms of another label ("tv" and "television")"""


class LabelsOut(BaseModel):
    labels: list[str]


class LabelError(Exception):
    pass


_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(timeout=TIMEOUT_S, max_retries=1, default_headers=config.ANTHROPIC_HEADERS)
    return _client


def _jpeg(image: Image.Image) -> str:
    small = image.copy()
    small.thumbnail((EDGE, EDGE))
    if small.mode != "RGB":
        small = small.convert("RGB")
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=85)
    return base64.standard_b64encode(buf.getvalue()).decode()


def clean(raw: list[str]) -> list[str]:
    """One general word per label ("desk lamp" -> "lamp" if the model slips), no repeats, at most MAX_LABELS."""
    labels = []
    for label in raw:
        words = label.lower().split()
        if words and words[-1] not in labels:
            labels.append(words[-1])
    return labels[:MAX_LABELS]


async def generate(image: Image.Image, category: str) -> list[str]:
    """The kinds of objects in `category` that are (or are likely) in this scene. Raises LabelError."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise LabelError("ANTHROPIC_API_KEY not set")
    data = await asyncio.to_thread(_jpeg, image)
    t0 = time.perf_counter()
    try:
        resp = await client().messages.parse(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                {"type": "text", "text": f"Category: {category}\nWhich objects in this category should be tracked here?"},
            ]}],
            output_format=LabelsOut,
        )
        out = resp.parsed_output
    except Exception as e:  # network, auth, rate limit or unparseable output
        raise LabelError(str(e).splitlines()[0] if str(e) else type(e).__name__) from e
    labels = clean(out.labels if out else [])
    log.info("labels from %s in %dms: %s", MODEL, (time.perf_counter() - t0) * 1000, labels)
    if not labels:
        raise LabelError("no labels returned")
    return labels


async def labels_or_fallback(image: Image.Image, category: str) -> list[str]:
    """generate(), but never raises: a Claude failure must not stop tracking, so it falls back to the
    category itself as the one SAM prompt (SAM takes noun phrases, so "kitchen utensils" still finds something)."""
    try:
        return await generate(image, category)
    except LabelError as e:
        log.warning("labels: Claude failed (%s); using the category %r as the label", e, category)
        return [category.lower()]


async def _cli(path: str, category: str):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(await generate(Image.open(path), category))


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else config.SAM_AUTO_CATEGORY))
