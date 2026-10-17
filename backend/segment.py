"""W3: cut the item out of the photo.

1. SAM (Baptiste's segmentation server, segmentation/server.py): send the photo with the item's
   category as the text prompt and take the detected box that overlaps Claude's box. It is much
   tighter than Claude's. Claude knows which object the user means; SAM knows its exact edges.
   A lone detection is used even without overlap; with several and none overlapping, Claude's
   box wins. The server returns boxes only (no masks yet), so the cutout is a crop, not the
   item on white.
2. Fallback: a padded crop around Claude's box, or a center crop when there is no usable box.

    python segment.py fixtures/lamp_room.jpg fixtures/item_lamp.json   # writes uploads/cli_cutout.jpg + cli_mask.jpg
"""
import asyncio
import io
import json
import logging
import sys
import time

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
SAM_TIMEOUT = 2.0    # seconds; vision already uses most of /detect's ~3s budget
MAX_EDGE = 1024      # saved images, and the photo sent to SAM, are capped at this size

RED, YELLOW = (255, 60, 60), (255, 210, 0)


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


async def sam_box(image: Image.Image, item: Item) -> tuple[BBox, float] | None:
    """SAM's box for the item and its score, or None (no server, failure, nothing usable). Never raises."""
    if not config.MODEL_SERVICE_URL or not item.category:
        return None
    small = image.copy()
    small.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    small.save(buf, format="JPEG", quality=85)
    headers = {"Content-Type": "image/jpeg"}
    if config.MODEL_SERVICE_TOKEN:
        headers["Authorization"] = f"Bearer {config.MODEL_SERVICE_TOKEN}"
    t0 = time.perf_counter()
    try:
        # wait_for makes SAM_TIMEOUT a hard cap; httpx's timeout only bounds each wait for data.
        r = await asyncio.wait_for(net.client().post(
            f"{config.MODEL_SERVICE_URL}/segment", params={"prompt": item.category},
            content=buf.getvalue(), headers=headers, timeout=SAM_TIMEOUT), SAM_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        W, H = data["width"], data["height"]
        found = [(pad(BBox(x=x0 / W, y=y0 / H, w=(x1 - x0) / W, h=(y1 - y0) / H), 0.0), float(o["score"]))
                 for o in data["objects"] for x0, y0, x1, y1 in [o["box"]]]
    except Exception as e:  # unreachable, too slow, error status, unexpected JSON
        log.warning("sam failed after %dms: %r", ms_since(t0), e)
        return None
    if not found:
        log.info("sam %dms: nothing found for %r", ms_since(t0), item.category)
        return None
    matches = [f for f in found if item.bbox and iou(f[0], item.bbox) > SAM_IOU]
    if matches:
        box, score = max(matches, key=lambda f: f[1])
    elif len(found) == 1:
        box, score = found[0]  # a lone detection: Claude's box was probably just off
    else:
        # Several candidates and none where Claude looked: a rough crop of the right item
        # beats a tight crop of the wrong one.
        log.info("sam %dms: %d objects, none overlap Claude's box; using Claude's box",
                 ms_since(t0), len(found))
        return None
    log.info("sam %dms: %d objects, %d overlap Claude's box, picked score=%.2f",
             ms_since(t0), len(found), len(matches), score)
    if box.w * box.h < SAM_MIN_AREA:
        return None
    return box, score


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


async def segment(image: Image.Image, item: Item, capture_id: str) -> Segmentation:
    """Never raises: SAM's box if usable, else Claude's padded box, else the center crop.

    The preview shows the cropped region in red and Claude's box in yellow.
    """
    t0 = time.perf_counter()
    sam = await sam_box(image, item)
    if sam:
        method, bbox, score = "sam3", sam[0], sam[1]
        crop = pad(bbox, SAM_PAD)
    else:
        method, score = "bbox_fallback", None
        bbox = crop = padded_box(item.bbox)
    try:
        cutout_path, mask_path = save_images(
            image, crop, capture_id, [(crop, RED)] + ([(item.bbox, YELLOW)] if item.bbox else []))
    except Exception:
        log.exception("segment %s: crop failed, using center crop", capture_id)
        method, score = "bbox_fallback", None
        bbox = crop = padded_box(None)
        cutout_path, mask_path = save_images(image, crop, capture_id, [(crop, RED)])
    return Segmentation(
        method=method, bbox=bbox, mask_score=score,
        cutout_path=cutout_path, cutout_url=f"{config.PUBLIC_BASE_URL}/{cutout_path}",
        mask_preview_url=f"{config.PUBLIC_BASE_URL}/{mask_path}", latency_ms=ms_since(t0),
    )


async def _cli(image_path: str, item_path: str):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    image = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    item = Item.model_validate(json.loads(open(item_path).read()))
    seg = await segment(image, item, "cli")
    print(seg.model_dump_json(indent=2))
    await net.close()


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1], sys.argv[2]))
