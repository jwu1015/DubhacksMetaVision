"""Crops of tracked objects: a box from SAM, padded a little, cut out of the frame it was seen in."""
import io

from PIL import Image

from storefront.models import BBox

SAM_PAD = 0.08   # SAM's box is tight, so it gets a little padding
MAX_EDGE = 1024  # crops are capped at this size


def pad(box: BBox, frac: float) -> BBox:
    """box plus frac of its size on each side, clamped to the image."""
    x0, y0 = max(0.0, box.x - box.w * frac), max(0.0, box.y - box.h * frac)
    x1, y1 = min(1.0, box.x + box.w * (1 + frac)), min(1.0, box.y + box.h * (1 + frac))
    return BBox(x=round(x0, 4), y=round(y0, 4), w=round(x1 - x0, 4), h=round(y1 - y0, 4))


def crop_image(image: Image.Image, crop: BBox, max_edge: int = MAX_EDGE) -> Image.Image:
    """The image cut to crop (normalized), at most max_edge px on its longest side."""
    W, H = image.size
    cutout = image.crop((round(crop.x * W), round(crop.y * H), round((crop.x + crop.w) * W),
                         round((crop.y + crop.h) * H)))
    cutout.thumbnail((max_edge, max_edge))
    return cutout


def sam_cutout(image: Image.Image, box: BBox, max_edge: int = MAX_EDGE) -> bytes:
    """The padded crop for a SAM box, as a JPEG kept in memory."""
    buf = io.BytesIO()
    crop_image(image, pad(box, SAM_PAD), max_edge).convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()
