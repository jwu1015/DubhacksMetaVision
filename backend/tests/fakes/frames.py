"""Frames built in code, so no test needs a video file."""
from PIL import Image


def image(width: int, height: int = 0) -> Image.Image:
    return Image.new("RGB", (width, height or width * 3 // 4), (90, 120, 150))
