"""A quick name and price range for an object's crop, from Claude Haiku ("black insulated water bottle",
$30-50), so a hotspot can show it before anyone taps."""
import logging

from pydantic import BaseModel

from storefront.pipeline import vlm

log = logging.getLogger("storefront.quick_label")

TIMEOUT_S = 6.0
EDGE = 512  # longest side of the crop sent

SYSTEM = """You label one product in a photo for a shopping app. Reply with:
- name: 2-5 words, lowercase: the color and kind of item, plus the brand only if a logo is clearly
  visible (e.g. "black insulated water bottle", "adidas long sleeve shirt", "fuzzy slippers")
- price_low, price_high: the usual price of a new one in US dollars, whole numbers"""


class QuickLabel(BaseModel):
    name: str
    price_low: int
    price_high: int


async def quick_label(jpeg: bytes) -> QuickLabel | None:
    """A short name and a price range for the item in this crop. Never raises: None if Claude fails."""
    try:
        q = await vlm.haiku.ask("quick_label", SYSTEM, [vlm.image(jpeg), {"type": "text", "text": "Label this item."}],
                                QuickLabel, max_tokens=200, timeout=TIMEOUT_S)
    except vlm.VLMError:
        return None
    name = " ".join(q.name.split())[:60]
    low, high = sorted((q.price_low, q.price_high))
    if not name or low <= 0:
        return None
    return QuickLabel(name=name, price_low=low, price_high=high)
