"""W3: find the item and cut it out of the photo, with SAM on Baptiste's segmentation server
(segmentation/server.py). The server returns boxes only (no masks yet), so a cutout is a crop.

SAM first (main.py, when a segmentation server is configured): sam_objects() with SAM_CATEGORIES
finds every object in view from its category list, and center_object() picks the one at the center of the view,
where glasses point. Claude then only describes that object for search.

Claude first (the fallback, segment()): Claude has picked the item; SAM is asked about Claude's
category and its box that overlaps Claude's is used, since it is much tighter. A lone detection is
used even without overlap; with several and none overlapping, Claude's box wins. Without a usable
SAM box: a padded crop around Claude's box, or a center crop when there is no usable box.

    python segment.py fixtures/lamp_room.jpg fixtures/item_lamp.json   # writes uploads/cli_cutout.jpg + cli_mask.jpg
"""
import asyncio
import io
import json
import logging
import math
import sys
import time
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageOps

import config
import net
from models import BBox, Item, Segmentation

log = logging.getLogger("gtl.segment")

PAD = 0.12           # Claude's box: padding on each side, as a fraction of its width/height
MIN_AREA = 0.01      # a Claude box under 1% of the image is more likely wrong than a tiny item: center crop
CENTER = 0.60        # the center crop covers 60% of each side
SAM_PAD = 0.08       # SAM's box is tight, so it gets less padding
SAM_MIN_AREA = 0.02  # a SAM box under 2% of the image is ignored
SAM_IOU = 0.3        # a SAM box must overlap Claude's this much to count as the same item
CENTER_MAX = 0.25    # SAM first: an object further than this from the center of the view isn't "looked at"
POINT_MAX = 0.10     # hint_point: an object further than this from the tapped point isn't the one tapped
POINT_CROP = 0.40    # hint_point with no SAM box there: the crop around the point covers 40% of each side
SAM_TIMEOUT = 2.0    # seconds, Claude first: SAM runs after Claude, inside /detect's budget
SAM_FIRST_TIMEOUT = 2.5  # seconds, SAM first: about Claude's own time, so a busy scene that makes SAM
                         # slow is no slower than going through Claude
# SAM first asks only about these: the server checks each category separately, so its full default
# list took ~1.9s per photo against ~0.8s for these 12 (17 Oct). Anything else falls back to Claude,
# which then asks SAM about its exact category. SAM_CATEGORIES="" uses the server's own list.
SAM_CATEGORIES = config.SAM_CATEGORIES
SAM_EDGE = 640       # the server resizes internally, so bigger photos only cost upload time
MAX_EDGE = 1024      # saved images are capped at this size

RED, YELLOW = (255, 60, 60), (255, 210, 0)


class Found(NamedTuple):
    box: BBox             # normalized to the photo
    score: float | None   # None for a box the app sent without one
    label: str            # the category that matched, e.g. "sofa"


def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


def pad(box: BBox, frac: float) -> BBox:
    """box plus frac of its size on each side, clamped to the image."""
    x0, y0 = max(0.0, box.x - box.w * frac), max(0.0, box.y - box.h * frac)
    x1, y1 = min(1.0, box.x + box.w * (1 + frac)), min(1.0, box.y + box.h * (1 + frac))
    return BBox(x=round(x0, 4), y=round(y0, 4), w=round(x1 - x0, 4), h=round(y1 - y0, 4))


def padded_box(bbox: BBox | None) -> BBox:
    """Claude's box plus PAD on each side; the center crop if the box is missing or tiny."""
    if bbox is None or bbox.w * bbox.h < MIN_AREA:
        m = (1 - CENTER) / 2
        return BBox(x=m, y=m, w=CENTER, h=CENTER)
    return pad(bbox, PAD)


def iou(a: BBox, b: BBox) -> float:
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / (a.w * a.h + b.w * b.h - inter)


_deleting: set[asyncio.Task] = set()  # one-frame sessions being deleted, kept so they aren't garbage-collected


async def _delete_session(sid: str):
    try:
        await net.client().delete(f"{config.MODEL_SERVICE_URL}/session/{sid}", headers=config.segmentation_headers(),
                                  timeout=5.0, follow_redirects=False)
    except Exception as e:  # it expires after 120s idle anyway
        log.info("sam: deleting one-frame session %s failed: %r", sid[:8], e)


async def _segment(jpeg: bytes, prompt: str | None, timeout: float):
    """The server's answer for one photo. Since 18 Oct its /segment only takes a category, and its Claude
    picks the labels (2-4.5s); a one-frame tracking session takes our labels as given and answers as fast
    as /segment used to (~0.3s). Without a prompt: /segment with SAM_AUTO_CATEGORY."""
    base, auth = config.MODEL_SERVICE_URL, config.segmentation_headers()
    image_headers = {"Content-Type": "image/jpeg", **auth}
    # No redirects: without a valid token Cloudflare redirects to a login page.
    if not prompt:
        return await net.client().post(f"{base}/segment", params={"category": config.SAM_AUTO_CATEGORY},
                                       content=jpeg, headers=image_headers, timeout=timeout, follow_redirects=False)
    r = await net.client().post(f"{base}/session", json={"prompt": prompt}, headers=auth, timeout=timeout,
                                follow_redirects=False)
    if r.status_code != 200:
        return r
    sid = r.json()["session_id"]
    try:
        return await net.client().post(f"{base}/session/{sid}/frame", content=jpeg, headers=image_headers,
                                       timeout=timeout, follow_redirects=False)
    finally:  # in the background, even when cancelled: the answer doesn't wait on it
        task = asyncio.create_task(_delete_session(sid))
        _deleting.add(task)
        task.add_done_callback(_deleting.discard)


async def sam_objects(image: Image.Image, prompt: str | None, timeout: float = SAM_TIMEOUT) -> list[Found] | None:
    """Every object SAM finds for prompt (comma-separated labels; None: labels the server's Claude picks
    for SAM_AUTO_CATEGORY), highest score first; None if the call failed. Never raises."""
    if not config.MODEL_SERVICE_URL:
        return None
    small = image.copy()
    small.thumbnail((SAM_EDGE, SAM_EDGE))
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=80)
    t0 = time.perf_counter()
    try:
        # wait_for makes the timeout a hard cap; httpx's timeout only bounds each wait for data.
        r = await asyncio.wait_for(_segment(buf.getvalue(), prompt, timeout), timeout)
        if r.status_code in (302, 401, 403):
            log.warning("sam: Cloudflare Access rejected the request (%d); check CF_ACCESS_CLIENT_ID/SECRET "
                        "and the server's access policy", r.status_code)
            return None
        r.raise_for_status()
        data = r.json()
        W, H = data["width"], data["height"]
        found = [Found(pad(BBox(x=x0 / W, y=y0 / H, w=(x1 - x0) / W, h=(y1 - y0) / H), 0.0),
                       float(o["score"]), o.get("prompt") or prompt or "item")
                 for o in data["objects"] for x0, y0, x1, y1 in [o["box"]]]
    except Exception as e:  # unreachable, too slow, error status, unexpected JSON
        log.warning("sam failed after %dms: %r", ms_since(t0), e)
        return None
    log.info("sam %dms: %d objects for %s", ms_since(t0), len(found), repr(prompt) if prompt else "the server's labels")
    return sorted(found, key=lambda f: -f.score)


def center_object(found: list[Found]) -> Found | None:
    """The object the user is looking at: the smallest box covering the center of the view (a lamp
    in front of a sofa, not the sofa), else the nearest box within CENTER_MAX. Tiny boxes are ignored."""
    def distance(b: BBox) -> float:  # from the center of the view to the box; 0 when the box covers it
        dx = max(b.x - 0.5, 0.0, 0.5 - (b.x + b.w))
        dy = max(b.y - 0.5, 0.0, 0.5 - (b.y + b.h))
        return math.hypot(dx, dy)

    usable = [f for f in found if f.box.w * f.box.h >= SAM_MIN_AREA and distance(f.box) <= CENTER_MAX]
    return min(usable, key=lambda f: (distance(f.box), f.box.w * f.box.h), default=None)


def point_object(found: list[Found], x: float, y: float) -> Found | None:
    """hint_point: the object the user tapped at (x, y), normalized. The smallest box containing the
    point (a lamp in front of a sofa, not the sofa), else the nearest box within POINT_MAX. Tiny boxes
    are ignored."""
    def distance(b: BBox) -> float:  # from the point to the box; 0 when the box contains it
        dx = max(b.x - x, 0.0, x - (b.x + b.w))
        dy = max(b.y - y, 0.0, y - (b.y + b.h))
        return math.hypot(dx, dy)

    usable = [f for f in found if f.box.w * f.box.h >= SAM_MIN_AREA and distance(f.box) <= POINT_MAX]
    return min(usable, key=lambda f: (distance(f.box), f.box.w * f.box.h), default=None)


def text_object(found: list[Found]) -> Found | None:
    """hint_text: of the objects SAM found for the user's words, the one at the center of the view,
    else the best-scoring one. Tiny boxes are ignored."""
    usable = [f for f in found if f.box.w * f.box.h >= SAM_MIN_AREA]
    return center_object(usable) or max(usable, key=lambda f: f.score, default=None)


def point_box(x: float, y: float) -> BBox:
    """hint_point with no SAM box there: POINT_CROP of each side around the point, moved inside the photo."""
    x0, y0 = (min(max(c - POINT_CROP / 2, 0.0), 1 - POINT_CROP) for c in (x, y))
    return BBox(x=round(x0, 4), y=round(y0, 4), w=POINT_CROP, h=POINT_CROP)


def point_crop(image: Image.Image, x: float, y: float, capture_id: str, t0: float) -> Segmentation:
    """The cutout around a tapped point when SAM has nothing there (or is down)."""
    crop = point_box(x, y)
    return result("bbox_fallback", crop, None, save_images(image, crop, capture_id, [(crop, RED)]), t0)


async def sam_box(image: Image.Image, item: Item) -> Found | None:
    """Claude first: SAM's box for Claude's item, or None (no server, failure, nothing usable)."""
    if not item.category:
        return None
    found = await sam_objects(image, item.category)
    if not found:
        return None
    matches = [f for f in found if item.bbox and iou(f.box, item.bbox) > SAM_IOU]
    if matches:
        pick = max(matches, key=lambda f: f.score)
    elif len(found) == 1:
        pick = found[0]  # a lone detection: Claude's box was probably just off
    else:
        # Several candidates and none where Claude looked: a rough crop of the right item
        # beats a tight crop of the wrong one.
        log.info("sam: %d objects, none overlap Claude's box; using Claude's box", len(found))
        return None
    log.info("sam: %d objects, %d overlap Claude's box, picked score=%.2f", len(found), len(matches), pick.score)
    return pick if pick.box.w * pick.box.h >= SAM_MIN_AREA else None


def save_images(image: Image.Image, crop: BBox, capture_id: str, draw: list[tuple[BBox, tuple]]) -> tuple[str, str]:
    """Write the cutout and the preview (photo with boxes drawn); return their uploads/ paths."""
    W, H = image.size

    def px(b: BBox):
        return round(b.x * W), round(b.y * H), round((b.x + b.w) * W), round((b.y + b.h) * H)

    cutout = image.crop(px(crop))
    cutout.thumbnail((MAX_EDGE, MAX_EDGE))
    cutout_path = f"uploads/{capture_id}_cutout.jpg"
    cutout.save(config.ROOT / cutout_path, quality=90)

    preview = image.copy()
    preview.thumbnail((MAX_EDGE, MAX_EDGE))
    s = preview.width / W
    pen = ImageDraw.Draw(preview)
    for box, color in draw:
        pen.rectangle([c * s for c in px(box)], outline=color, width=4)
    mask_path = f"uploads/{capture_id}_mask.jpg"
    preview.save(config.ROOT / mask_path, quality=80)
    return cutout_path, mask_path


def result(method: str, bbox: BBox, score: float | None, paths: tuple[str, str], t0: float) -> Segmentation:
    cutout_path, mask_path = paths
    return Segmentation(
        method=method, bbox=bbox, mask_score=score,
        cutout_path=cutout_path, cutout_url=f"{config.PUBLIC_BASE_URL}/{cutout_path}",
        mask_preview_url=f"{config.PUBLIC_BASE_URL}/{mask_path}", latency_ms=ms_since(t0),
    )


def sam_crop(image: Image.Image, found: Found, capture_id: str, t0: float, rough: BBox | None = None) -> Segmentation:
    """The cutout for a SAM box: the box plus SAM_PAD. The preview shows the crop in red and, if
    given, Claude's box in yellow."""
    crop = pad(found.box, SAM_PAD)
    paths = save_images(image, crop, capture_id, [(crop, RED)] + ([(rough, YELLOW)] if rough else []))
    return result("sam3", found.box, found.score, paths, t0)


async def segment(image: Image.Image, item: Item, capture_id: str, use_sam: bool = True) -> Segmentation:
    """Claude first. Never raises: SAM's box if usable, else Claude's padded box, else the center crop.
    use_sam=False skips SAM (e.g. it just failed for this photo)."""
    t0 = time.perf_counter()
    found = await sam_box(image, item) if use_sam else None
    try:
        if found:
            return sam_crop(image, found, capture_id, t0, rough=item.bbox)
        crop = padded_box(item.bbox)
        paths = save_images(image, crop, capture_id, [(crop, RED)] + ([(item.bbox, YELLOW)] if item.bbox else []))
        return result("bbox_fallback", crop, None, paths, t0)
    except Exception:
        log.exception("segment %s: crop failed, using center crop", capture_id)
        crop = padded_box(None)
        return result("bbox_fallback", crop, None, save_images(image, crop, capture_id, [(crop, RED)]), t0)


async def _cli(image_path: str, item_path: str):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    image = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    item = Item.model_validate(json.loads(open(item_path).read()))
    seg = await segment(image, item, "cli")
    print(seg.model_dump_json(indent=2))
    await net.close()


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1], sys.argv[2]))
