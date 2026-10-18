"""A quick name and price range for an object's crop, from Claude Haiku ("black insulated water bottle",
$30-50), so a hotspot can show it before anyone taps."""
import logging

from pydantic import BaseModel

from storefront.pipeline import vlm

log = logging.getLogger("storefront.quick_label")

TIMEOUT_S = 6.0
EDGE = 512  # longest side of the crop sent

SYSTEM = """You label one product in a photo for a shopping app. Reply with:
- name: 2-5 words, lowercase. Say what it is as specifically as the photo allows: the brand and product
  when a printed name, a logo or recognizable packaging shows it, even partly (e.g. "sony wh-1000xm5
  headphones", "adidas long sleeve shirt"); otherwise the color and kind of item ("black insulated
  water bottle", "fuzzy slippers"). Don't invent a brand nothing in the photo shows. Use the item's
  everyday name ("can", "mug", "lamp"), never a shape ("cylinder", "box", "object").
- price_low, price_high: the usual price of a new one in US dollars, whole numbers
You are told what the user is looking for (a category, e.g. "electronics") and what the tracker took
this object for. Label the object in that category that the crop shows: in "electronics", the phone,
not its case or the desk under it. If the crop holds several things, label the one that fits."""


def context(category: str | None, label: str | None) -> str:
    """The lines before the question: what the user is looking for, and what the tracker found."""
    lines = [f"The user is looking for: {category}" if category else "",
             f'The tracker found this as: "{label}"' if label else ""]
    return "".join(line + "\n" for line in lines if line)


class QuickLabel(BaseModel):
    name: str
    price_low: int
    price_high: int


async def quick_label(jpeg: bytes, category: str | None = None, label: str | None = None) -> QuickLabel | None:
    """A short name and a price range for the item in this crop. category: what the user is looking for;
    label: SAM's prompt that found it. Never raises: None if Claude fails."""
    text = context(category, label) + "Label this item."
    try:
        q = await vlm.haiku.ask("quick_label", SYSTEM, [vlm.image(jpeg), {"type": "text", "text": text}],
                                QuickLabel, max_tokens=200, timeout=TIMEOUT_S)
    except vlm.VLMError:
        return None
    name = " ".join(q.name.split())[:60]
    low, high = sorted((q.price_low, q.price_high))
    if not name or low <= 0:
        return None
    return QuickLabel(name=name, price_low=low, price_high=high)
