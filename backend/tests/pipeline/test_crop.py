"""Tests for crop.py."""
import io

from PIL import Image

from storefront.models import BBox
from storefront.pipeline import crop


def test_pad_stays_inside_the_image():
    assert crop.pad(BBox(x=0.1, y=0.2, w=0.4, h=0.4), 0.5) == BBox(x=0.0, y=0.0, w=0.7, h=0.8)


def test_crop_image_is_capped():
    big = Image.new("RGB", (4000, 3000))
    out = crop.crop_image(big, BBox(x=0, y=0, w=1, h=1))
    assert max(out.size) == crop.MAX_EDGE


def test_sam_cutout_is_a_padded_jpeg_in_memory():
    frame = Image.new("RGB", (1000, 1000), (10, 20, 30))
    jpeg = crop.sam_cutout(frame, BBox(x=0.4, y=0.4, w=0.2, h=0.2))
    assert Image.open(io.BytesIO(jpeg)).size == (232, 232)  # 200 px plus 8% on each side
