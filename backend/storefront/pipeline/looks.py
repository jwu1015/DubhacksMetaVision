"""Looks: what a tracked object is and where to buy it, one Look per uid.

Claude describes the object's crop, then the stores are searched on Claude's queries; each step goes out
as a "look" message (models.LookMessage). At most MAX_LOOKS run at once: the rest wait, most recent first.

The crop stays in memory until the user picks the object (select): only then is it written to uploads/,
and the Look's messages carry its URL. Google Lens needs that public URL, so a Look only searches Lens
when the object was picked before its search started.
"""
import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from storefront import config
from storefront.models import BBox, Item, LookMessage, LookStage, SearchResponse
from storefront.pipeline import pricing, rank, rerank, search, vision

log = logging.getLogger("storefront.looks")

FAILED = "Couldn't get prices for that one."


def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


async def search_item(item: Item, cutout: str | bytes, look_id: str) -> SearchResponse:
    """search -> rerank -> pricing + rank. cutout: the item's crop, by URL, or the crop itself."""
    t0 = time.perf_counter()
    timing: dict[str, int] = {}
    products = await search.search_all(cutout, item, capture_id=look_id)
    timing["search"] = ms_since(t0)
    products = await rerank.rerank(cutout, products)
    timing["rerank"] = ms_since(t0) - timing["search"]
    resp = await rank.choose(item, products, pricing.compute_price_insight(products))
    resp.capture_id = look_id
    resp.timing_ms = timing | {"rank": ms_since(t0) - timing["search"] - timing["rerank"], "total": ms_since(t0)}
    log.info("search %s products=%d timing=%s", look_id, len(products), resp.timing_ms)
    return resp


@dataclass
class Look:
    uid: str
    label: str            # SAM's prompt
    box: BBox             # the object's box in the frame it was cropped from
    cutout: bytes         # the crop, a JPEG in memory
    category: str | None = None  # what the user was looking for when it started
    look_id: str = field(default_factory=lambda: "l_" + uuid.uuid4().hex[:8])
    stage: LookStage = "started"
    item: Item | None = None
    search: SearchResponse | None = None
    message: str | None = None
    selected: bool = False
    picked_at: int | None = None  # Unix ms of the latest pick
    crop_url: str | None = None
    task: asyncio.Task | None = None

    def message_dict(self) -> dict:
        return LookMessage(uid=self.uid, look_id=self.look_id, stage=self.stage, label=self.label,
                           box=self.box.corners(), selected=self.selected, picked_at=self.picked_at,
                           crop_url=self.crop_url,
                           item=self.item, search=self.search, message=self.message).model_dump(mode="json")


class Looks:
    def __init__(self, publish: Callable[[dict], None], max_running: int = config.MAX_LOOKS):
        self.publish, self.max_running = publish, max_running
        self.looks: dict[str, Look] = {}
        self._waiting: list[Look] = []  # started after the limit was reached, oldest first
        self._last_pick = 0  # the latest picked_at

    def get(self, uid: str) -> Look | None:
        return self.looks.get(uid)

    def start(self, uid: str, label: str, box: BBox, cutout: bytes, category: str | None = None) -> Look:
        """A Look on this object, unless it has one. category: what the user is looking for."""
        look = self.looks.get(uid)
        if look:
            return look
        look = self.looks[uid] = Look(uid, label.strip() or "item", box, cutout, category)
        self._send(look)
        if self._running() < self.max_running:
            self._run(look)
        else:
            self._waiting.append(look)
        return look

    def select(self, uid: str) -> Look | None:
        """The user picked this object: write its crop, so its messages carry the crop's URL. Picking it again
        sends it again with a new picked_at, so the viewer shows it over the picks since. None if it has no
        Look."""
        look = self.looks.get(uid)
        if look is None:
            return None
        look.picked_at = self._last_pick = max(time.time_ns() // 1_000_000, self._last_pick + 1)  # always the latest
        if not look.selected:
            path = config.UPLOADS / f"{look.look_id}.jpg"
            path.write_bytes(look.cutout)
            look.selected, look.crop_url = True, f"{config.PUBLIC_BASE_URL}/uploads/{path.name}"
            if look in self._waiting:  # the user is waiting on this one now
                self._waiting.remove(look)
                self._run(look)
        log.info("look %s on %s: picked", look.look_id, uid)
        self._send(look)
        return look

    def forget(self, uid: str):
        """The object left scene memory: stop its Look, unless the user picked it."""
        look = self.looks.get(uid)
        if look and not look.selected:
            self._drop(look)

    def reset(self):
        """Tracking restarted (pause, camera switch, disconnect): stop every Look nobody picked."""
        for look in [lk for lk in self.looks.values() if not lk.selected]:
            self._drop(look)

    def _running(self) -> int:
        return sum(1 for lk in self.looks.values() if lk.task and not lk.task.done())

    def _run(self, look: Look):
        look.task = asyncio.create_task(self._go(look))
        look.task.add_done_callback(lambda _: self._next())

    def _next(self):
        while self._waiting and self._running() < self.max_running:
            self._run(self._waiting.pop())  # the most recent first

    def _drop(self, look: Look):
        del self.looks[look.uid]
        if look in self._waiting:
            self._waiting.remove(look)
        if look.task:
            look.task.cancel()

    def _send(self, look: Look):
        self.publish(look.message_dict())

    async def _go(self, look: Look):
        """Describe the crop, then search on Claude's queries; Claude failing leaves the label."""
        t0 = time.perf_counter()
        try:
            item = await vision.detect(look.cutout, category=look.category, label=look.label)
        except Exception as e:
            log.warning("look %s: Claude failed (%r); searching with the label", look.look_id, e)
            item = None
        if item is not None and item.category:
            look.item, look.stage = item, "described"
            self._send(look)
        else:
            item = Item(category=look.label, search_queries=[look.label])
        log.info("look %s on %s: described as %r in %dms", look.look_id, look.uid, item.category, ms_since(t0))
        try:
            look.search = await search_item(item, look.crop_url or look.cutout, look.look_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("look %s: search failed", look.look_id)
            look.stage, look.message = "failed", FAILED
        else:
            look.search.item = look.item
            look.stage = "results"
        self._send(look)
