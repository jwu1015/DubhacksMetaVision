"""Can the wearer record here? Claude Haiku looks at a tracking session's first frame, alongside the label
call (tracker.py), and a "no" ends the session: the phone shows the reason.

    python -m storefront.live.security fixtures/lamp_room.jpg
"""
import asyncio
import logging
import sys

from PIL import Image
from pydantic import BaseModel

from storefront.pipeline import vlm

log = logging.getLogger("storefront.security")

TIMEOUT_S = 10.0  # the label call has the same budget, and the session waits on both
EDGE = 512

SYSTEM = """\
You check whether it is OK to record video in a place, from one frame taken by smart glasses or a phone.
Answer allowed=false only when recording here is clearly not OK, e.g.:
- a private or intimate space: bathroom, toilet, locker room, changing room, fitting room
- a sign or notice forbidding photos, video or recording
- a place where recording is typically restricted: a hospital ward or exam room, a courtroom, a polling
  station, a security checkpoint, a classroom full of children
- sensitive information in full view: a screen or document with passwords, card numbers, medical or
  personal records
- a person who is undressed
Otherwise (homes, stores, offices, streets, ordinary rooms), allowed=true. When unsure, allow it.
reason: one short sentence the user will read, e.g. "This looks like a changing room." Empty when allowed."""


class Verdict(BaseModel):
    allowed: bool
    reason: str = ""


async def check(image: Image.Image) -> Verdict:
    """Whether recording is OK where this frame was taken. Never raises: if Claude fails, recording is
    allowed (logged), so a Claude outage doesn't end every session."""
    try:
        content = [await asyncio.to_thread(vlm.image, image, EDGE),
                   {"type": "text", "text": "Can I record here?"}]
        v = await vlm.haiku.ask("security", SYSTEM, content, Verdict, max_tokens=200, timeout=TIMEOUT_S, attempts=2)
    except vlm.VLMError as e:
        log.warning("security: Claude failed (%s); allowing the session", e)
        return Verdict(allowed=True)
    reason = " ".join(v.reason.split())[:200] or "Recording isn't allowed here."
    log.info("security: %s", "allowed" if v.allowed else f"blocked ({reason})")
    return Verdict(allowed=True) if v.allowed else Verdict(allowed=False, reason=reason)


async def _cli(path: str):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(await check(Image.open(path)))
    await vlm.haiku.close()


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1]))
