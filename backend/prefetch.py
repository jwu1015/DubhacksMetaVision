"""Instant labels and background Looks for the objects the live stream tracks.

stream.py hands every tracking result to observe():
- An object in view for QUICK_AFTER_S gets a quick name and a price range from Claude Haiku, once per object
  ("black insulated water bottle", $30-50). fields() adds them to the objects stream.py sends to the phone
  and the projector, so a hotspot can show them before anyone taps.
- The PREFETCH_MAX objects in view the longest (at least PREFETCH_AFTER_S) get the whole Look in the
  background through start_look, which is main.py's background_look(): Claude describes the object's crop,
  then the stores are searched on Claude's queries. When the user picks one of them (a tap, or the voice
  assistant), main.py's look() takes that Look (claim()) instead of starting a new one.

Nothing here is written to disk or announced. Crops exist only in memory, while Claude (and match.py's
score) look at them. What stays, by track id, is what came out of them: the tracker's label, the quick
label, and the background Look's description and results. An object is forgotten FORGET_S after it was
last seen, as scene memory forgets it, and everything goes on reset() (stream.py's purge: a pause, a
camera switch, the phone disconnecting).

PREFETCH=false turns both off. A background Look costs about 3 SerpAPI searches; quick labels cost none.
"""
import asyncio
import io
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image
from pydantic import BaseModel

import config
import vlm
from models import BBox
from scene import WINDOW_S
from tracker import TrackResult

log = logging.getLogger("gtl.prefetch")

ENABLED = os.getenv("PREFETCH", "true").strip().lower() in ("1", "true", "yes") and not config.USE_FIXTURES
QUICK_AFTER_S = 0.5      # in view this long before it gets a quick label, so flicker costs nothing
PREFETCH_AFTER_S = 2.0   # in view this long before its whole Look starts in the background
PREFETCH_MAX = 3         # background Looks among the objects in view
FORGET_S = WINDOW_S      # an object out of view this long is forgotten, as scene memory forgets it
CROP_EDGE = 512          # longest side of the crop sent for a quick label
PAD = 0.08               # around the box, as a fraction of its width and height
TIMEOUT_S = 6.0

SYSTEM = """You label one product in a photo for a shopping app. Reply with:
- name: 2-5 words, lowercase: the color and kind of item, plus the brand only if a logo is clearly
  visible (e.g. "black insulated water bottle", "adidas long sleeve shirt", "fuzzy slippers")
- price_low, price_high: the usual price of a new one in US dollars, whole numbers"""


class QuickLabel(BaseModel):
    name: str
    price_low: int
    price_high: int



@dataclass
class Background:
    """A background Look (main.py's background_look). Its crop is in memory only, and gone once both are done."""
    describe: asyncio.Task  # -> Claude's Item for the crop
    search: asyncio.Task    # -> the SearchResponse for that item, with capture_id "" (no capture yet)
    claimed: bool = False   # the user picked the object, and their Look awaits these tasks

    def cancel(self):
        """Stop the work, unless a Look has claimed it."""
        if not self.claimed:
            self.describe.cancel()
            self.search.cancel()

    @property
    def failed(self) -> bool:
        return self.search.done() and (self.search.cancelled() or self.search.exception() is not None)


# Set by main.py: starts a background Look on a tracked object; None if scene memory has no frame of it.
start_look: Callable[[int], Background | None] | None = None
clock = time.monotonic


@dataclass
class Seen:
    category: str                       # the tracker's label; a different one under the same id is a new object
    first: float
    last: float
    label: QuickLabel | None = None
    labeling: asyncio.Task | None = None
    look: Background | None = None


seen: dict[int, Seen] = {}
_last_frame_idx = -1


def ms_since(t0: float) -> int:
    return round((clock() - t0) * 1000)


# ---------- quick labels ----------

def crop_jpeg(frame: Image.Image, box: BBox) -> bytes:
    W, H = frame.size
    x0, y0 = max(0.0, box.x - box.w * PAD), max(0.0, box.y - box.h * PAD)
    x1, y1 = min(1.0, box.x + box.w * (1 + PAD)), min(1.0, box.y + box.h * (1 + PAD))
    img = frame.crop((round(x0 * W), round(y0 * H), round(x1 * W), round(y1 * H))).convert("RGB")
    img.thumbnail((CROP_EDGE, CROP_EDGE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


async def quick_label(jpeg: bytes) -> QuickLabel | None:
    """A short name and a price range for the item in this crop. Never raises: None if Claude fails."""
    try:
        q = await vlm.haiku.ask("prefetch", SYSTEM, [vlm.image(jpeg), {"type": "text", "text": "Label this item."}],
                                QuickLabel, max_tokens=200, timeout=TIMEOUT_S)
    except vlm.VLMError:
        return None
    name = " ".join(q.name.split())[:60]
    low, high = sorted((q.price_low, q.price_high))
    if not name or low <= 0:
        return None
    return QuickLabel(name=name, price_low=low, price_high=high)


async def _label(track_id: int, s: Seen, jpeg: bytes):
    t0 = clock()
    s.label = await quick_label(jpeg)
    log.info("prefetch: t%d labeled %r in %dms", track_id, s.label.name if s.label else None, ms_since(t0))


# ---------- what stream.py calls ----------

def _drop(s: Seen):
    if s.labeling and not s.labeling.done():
        s.labeling.cancel()
    if s.look:
        s.look.cancel()


def reset():
    """Forget every object, and stop its work (stream.py's purge)."""
    global _last_frame_idx
    for s in seen.values():
        _drop(s)
    seen.clear()
    _last_frame_idx = -1


def observe(result: TrackResult, frame: Image.Image):
    """Every tracking result: start quick labels and background Looks that are due."""
    global _last_frame_idx
    if not ENABLED:
        return
    if result.frame_idx < _last_frame_idx:  # a new tracking session numbers its objects from scratch
        reset()
    _last_frame_idx = result.frame_idx
    now = clock()
    visible = []
    for o in result.objects:
        s = seen.get(o.id)
        if s is None or s.category != o.label:
            if s is not None:
                _drop(s)
            s = seen[o.id] = Seen(category=o.label, first=now, last=now)
        s.last = now
        visible.append(o.id)
        if s.labeling is None and now - s.first >= QUICK_AFTER_S:
            s.labeling = asyncio.create_task(_label(o.id, s, crop_jpeg(frame, o.box)))

    if start_look is not None:  # the objects in view the longest, up to PREFETCH_MAX of them
        looked = sum(1 for i in visible if seen[i].look is not None)
        ready = sorted((i for i in visible if seen[i].look is None and now - seen[i].first >= PREFETCH_AFTER_S),
                       key=lambda i: seen[i].first)
        for i in ready[:max(0, PREFETCH_MAX - looked)]:
            if (look := start_look(i)) is not None:
                seen[i].look = look
                log.info("prefetch: background Look on t%d", i)

    for i in [i for i, s in seen.items() if now - s.last > FORGET_S]:
        _drop(seen.pop(i))


def fields(track_id: int) -> dict:
    """The quick label for an object, as extra fields on the objects stream.py sends out; {} until it's known."""
    s = seen.get(track_id)
    if s is None or s.label is None:
        return {}
    return {"name": s.label.name, "price_low": s.label.price_low, "price_high": s.label.price_high}


def claim(track_id: int) -> Background | None:
    """This object's background Look, done or still running; None if it has none, or its search failed.
    The caller's Look awaits it from then on, so forgetting the object or reset() no longer cancels it."""
    s = seen.get(track_id)
    if s is None or s.look is None or s.look.failed:
        return None
    s.look.claimed = True
    return s.look
