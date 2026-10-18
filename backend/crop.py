"""Crops of tracked objects: a box from SAM, padded a little, cut out of the frame it was seen in."""
import io
import time
from typing import NamedTuple

from PIL import Image, ImageDraw

import config
from models import BBox, Segmentation

SAM_PAD = 0.08   # SAM's box is tight, so it gets a little padding
MAX_EDGE = 1024  # crops and previews are capped at this size

RED = (255, 60, 60)


class Found(NamedTuple):
    box: BBox             # normalized to the frame
    score: float | None
    label: str            # SAM's prompt that matched, e.g. "lamp"


def pad(box: BBox, frac: float) -> BBox:
    """box plus frac of its size on each side, clamped to the image."""
    x0, y0 = max(0.0, box.x - box.w * frac), max(0.0, box.y - box.h * frac)
    x1, y1 = min(1.0, box.x + box.w * (1 + frac)), min(1.0, box.y + box.h * (1 + frac))
    return BBox(x=round(x0, 4), y=round(y0, 4), w=round(x1 - x0, 4), h=round(y1 - y0, 4))


def crop_image(image: Image.Image, crop: BBox) -> Image.Image:
    """The image cut to crop (normalized), at most MAX_EDGE px on its longest side."""
    W, H = image.size
    cutout = image.crop((round(crop.x * W), round(crop.y * H), round((crop.x + crop.w) * W),
                         round((crop.y + crop.h) * H)))
    cutout.thumbnail((MAX_EDGE, MAX_EDGE))
    return cutout


def sam_cutout(image: Image.Image, box: BBox) -> bytes:
    """The padded crop for a SAM box as a JPEG, kept in memory only."""
    buf = io.BytesIO()
    crop_image(image, pad(box, SAM_PAD)).convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def sam_crop(image: Image.Image, found: Found, capture_id: str, t0: float) -> Segmentation:
    """Write the padded crop and a preview with the crop drawn in red to uploads/."""
    crop = pad(found.box, SAM_PAD)
    cutout_path, preview_path = f"uploads/{capture_id}_cutout.jpg", f"uploads/{capture_id}_mask.jpg"
    crop_image(image, crop).save(config.ROOT / cutout_path, quality=90)
    preview = image.copy()
    preview.thumbnail((MAX_EDGE, MAX_EDGE))
    W, H = preview.size
    ImageDraw.Draw(preview).rectangle(
        [crop.x * W, crop.y * H, (crop.x + crop.w) * W, (crop.y + crop.h) * H], outline=RED, width=4)
    preview.save(config.ROOT / preview_path, quality=80)
    return Segmentation(
        method="sam3", bbox=found.box, mask_score=found.score, cutout_path=cutout_path,
        cutout_url=f"{config.PUBLIC_BASE_URL}/{cutout_path}",
        mask_preview_url=f"{config.PUBLIC_BASE_URL}/{preview_path}",
        latency_ms=round((time.perf_counter() - t0) * 1000))
