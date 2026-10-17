"""W3: cut the item out of the photo.

For now this is the bbox fallback only: a padded crop around Claude's box. The SAM step
(Baptiste's segmentation/server.py) slots in front of it once that API settles.

    python segment.py fixtures/lamp_room.jpg fixtures/item_lamp.json   # writes uploads/cli_cutout.jpg + cli_mask.jpg
"""
import asyncio
import json
import logging
import sys
import time

from PIL import Image, ImageDraw, ImageOps

import config
from models import BBox, Item, Segmentation

log = logging.getLogger("gtl.segment")

PAD = 0.12       # padding on each side, as a fraction of the box's width/height
MIN_AREA = 0.01  # a box under 1% of the image is more likely wrong than a tiny item: use the center crop
CENTER = 0.60    # the center crop covers 60% of each side
MAX_EDGE = 1024  # saved images are capped at this size (they go out through ngrok)


def padded_box(bbox: BBox | None) -> BBox:
    """Claude's box plus PAD on each side, clamped to the image; center crop if missing or tiny."""
    if bbox is None or bbox.w * bbox.h < MIN_AREA:
        m = (1 - CENTER) / 2
        return BBox(x=m, y=m, w=CENTER, h=CENTER)
    x0, y0 = max(0.0, bbox.x - bbox.w * PAD), max(0.0, bbox.y - bbox.h * PAD)
    x1, y1 = min(1.0, bbox.x + bbox.w * (1 + PAD)), min(1.0, bbox.y + bbox.h * (1 + PAD))
    return BBox(x=round(x0, 4), y=round(y0, 4), w=round(x1 - x0, 4), h=round(y1 - y0, 4))


def bbox_crop(image: Image.Image, box: BBox, capture_id: str, t0: float) -> Segmentation:
    W, H = image.size
    px = (round(box.x * W), round(box.y * H), round((box.x + box.w) * W), round((box.y + box.h) * H))
    cutout = image.crop(px)
    cutout.thumbnail((MAX_EDGE, MAX_EDGE))
    cutout_path = f"uploads/{capture_id}_cutout.jpg"
    cutout.save(config.ROOT / cutout_path, quality=90)

    # Preview for the app/debugging: the photo with the crop drawn on it.
    preview = image.copy()
    preview.thumbnail((MAX_EDGE, MAX_EDGE))
    s = preview.width / W
    ImageDraw.Draw(preview).rectangle([c * s for c in px], outline=(255, 60, 60), width=4)
    mask_path = f"uploads/{capture_id}_mask.jpg"
    preview.save(config.ROOT / mask_path, quality=80)

    return Segmentation(
        method="bbox_fallback", bbox=box, mask_score=None,
        cutout_path=cutout_path, cutout_url=f"{config.PUBLIC_BASE_URL}/{cutout_path}",
        mask_preview_url=f"{config.PUBLIC_BASE_URL}/{mask_path}",
        latency_ms=round((time.perf_counter() - t0) * 1000),
    )


async def segment(image: Image.Image, item: Item, capture_id: str) -> Segmentation:
    """Never raises: a bad box falls back to the center crop."""
    t0 = time.perf_counter()
    try:
        return bbox_crop(image, padded_box(item.bbox), capture_id, t0)
    except Exception:
        log.exception("segment %s: bbox crop failed, using center crop", capture_id)
        return bbox_crop(image, padded_box(None), capture_id, t0)


async def _cli(image_path: str, item_path: str):
    image = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    item = Item.model_validate(json.loads(open(item_path).read()))
    seg = await segment(image, item, "cli")
    print(seg.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1], sys.argv[2]))
