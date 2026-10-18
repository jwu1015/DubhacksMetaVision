"""Which objects SAM should look for: Claude picks them from a frame, within a category (moved here from
the segmentation server on 18 Oct; the server now only takes labels).

tracker.py asks before starting a session, whenever SAM_CATEGORIES is empty; the session starts once
the labels are back.

    python labels.py fixtures/lamp_room.jpg "furniture and home decor"
"""
import asyncio
import logging
import os
import sys

from PIL import Image
from pydantic import BaseModel

import config
import vlm

log = logging.getLogger("gtl.labels")

MAX_LABELS = int(os.getenv("MAX_LABELS", "12"))
TIMEOUT_S = 10.0  # both attempts; the session waits on this, so fail fast: the category is the fallback
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


def clean(raw: list[str]) -> list[str]:
    """One general word per label ("desk lamp" -> "lamp" if the model slips), no repeats, at most MAX_LABELS."""
    labels = []
    for label in raw:
        words = label.lower().split()
        if words and words[-1] not in labels:
            labels.append(words[-1])
    return labels[:MAX_LABELS]


async def generate(image: Image.Image, category: str) -> list[str]:
    """The kinds of objects in `category` that are (or are likely) in this scene. Raises vlm.VLMError."""
    content = [await asyncio.to_thread(vlm.image, image, EDGE),
               {"type": "text", "text": f"Category: {category}\nWhich objects in this category should be tracked here?"}]
    out = await vlm.haiku.ask("labels", SYSTEM, content, LabelsOut, max_tokens=1024, timeout=TIMEOUT_S, attempts=2)
    labels = clean(out.labels)
    log.info("labels for %r: %s", category, labels)
    if not labels:
        raise vlm.VLMError("no labels returned")
    return labels


async def labels_or_fallback(image: Image.Image, category: str) -> list[str]:
    """generate(), but never raises: a Claude failure must not stop tracking, so it falls back to the
    category itself as the one SAM prompt (SAM takes noun phrases, so "kitchen utensils" still finds something)."""
    try:
        return await generate(image, category)
    except vlm.VLMError as e:
        log.warning("labels: Claude failed (%s); using the category %r as the label", e, category)
        return [category.lower()]


async def _cli(path: str, category: str):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(await generate(Image.open(path), category))
    await vlm.haiku.close()


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else config.SAM_AUTO_CATEGORY))
