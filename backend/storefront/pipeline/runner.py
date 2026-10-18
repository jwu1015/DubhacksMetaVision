"""Runs the pipeline on each object the stream tracks: once its uid has been on screen for
QUICK_LABEL_DWELL_MS it gets a quick label, and once on screen for LOOK_DWELL_MS, a Look. Both crop the
object from its best frame so far in scene memory (the sharpest, biggest and most centered: scene.py), not
from the frame that triggered them, which the camera moving may have blurred. The user picking an object
(select) starts its Look at once if its timer hasn't yet.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

from PIL import Image

from storefront.live.stream import Stream
from storefront.live.tracker import TrackedObject, TrackResult
from storefront.live.trigger import Dwell
from storefront.models import BBox, LabelMessage
from storefront.pipeline import crop, quick_label
from storefront.pipeline.looks import Look, Looks

log = logging.getLogger("storefront.runner")


class Runner:
    def __init__(self, live: Stream, looks: Looks, dwell: Dwell,
                 label: Callable[[bytes, str, str], Awaitable[quick_label.QuickLabel | None]] = quick_label.quick_label):
        self.live, self.looks, self.dwell, self.label = live, looks, dwell, label
        self._labeling: dict[str, asyncio.Task] = {}

    def observe(self, result: TrackResult, frame: Image.Image):
        """Every tracking result: start the quick labels and Looks that are due, and stop the Looks of
        objects scene memory has forgotten."""
        on_screen = {o.uid: o for o in result.objects}
        label_due, look_due = self.dwell.update(list(on_screen), result.captured_at)
        for uid in label_due:
            o = on_screen[uid]
            self._labeling[uid] = asyncio.create_task(self._quick_label(o, *self.view(o, frame)))
        for uid in look_due:
            o = on_screen[uid]
            self._start_look(o, *self.view(o, frame))
        for uid in [u for u in self.looks.looks if u not in self.live.scene.tracks]:
            self.looks.forget(uid)

    def view(self, o: TrackedObject, frame: Image.Image) -> tuple[Image.Image, BBox]:
        """The frame to crop an object from and its box in it: its best frame so far in scene memory, else
        this one."""
        best = self.live.scene.best(o.uid)
        return (best[0], best[1].frame_box) if best else (frame, o.box)

    def select(self, uid: str) -> Look | None:
        """The user picked this object: its Look, started now if its timer hadn't yet. None if scene memory
        doesn't hold it (any more)."""
        if self.looks.get(uid) is None:
            best = self.live.scene.best(uid)
            if best is None:
                log.info("runner: select %s: not in scene memory", uid)
                return None
            frame, track = best
            self.dwell.looked(uid)
            self.looks.start(uid, track.label, track.frame_box, crop.sam_cutout(frame, track.frame_box),
                             self.live.tracker.category)
        return self.looks.select(uid)

    def reset(self):
        """Tracking restarted: forget every object and stop its work, except the Looks the user picked."""
        for task in self._labeling.values():
            task.cancel()
        self._labeling.clear()
        self.dwell.reset()
        self.looks.reset()

    def _start_look(self, o: TrackedObject, frame: Image.Image, box: BBox):
        self.looks.start(o.uid, o.label, box, crop.sam_cutout(frame, box), self.live.tracker.category)

    async def _quick_label(self, o: TrackedObject, frame: Image.Image, box: BBox):
        jpeg = await asyncio.to_thread(crop.sam_cutout, frame, box, quick_label.EDGE)
        q = await self.label(jpeg, self.live.tracker.category, o.label)
        self._labeling.pop(o.uid, None)
        log.info("runner: %s labeled %r", o.uid, q.name if q else None)
        if q:
            self.live.publish(LabelMessage(uid=o.uid, box=o.box.corners(), name=q.name, price_low=q.price_low,
                                           price_high=q.price_high).model_dump(mode="json"))
