"""Live view: frames in, tracked boxes out, shown on the projector page.

The phone's video (rtc/legacy_webrtc.py) only calls live.push_frame(img).
The tracking loop sends the newest frame to Baptiste's server, waits for the answer and repeats,
so frames that arrive meanwhile are skipped, never queued, and the video never waits on tracking.
Each answer goes into scene memory (scene.py) and out to the projector page (and any other subscriber)
as an "objects" event, listing every object scene memory holds, those in view first:

    {"type": "objects", "frame_idx", "width", "height", "age_ms",
     "objects": [{"id", "label", "score", "box": {"x", "y", "w", "h"},       (box normalized 0-1)
                  "in_view", "centered", "seconds_viewed", "first_seen_ts", "last_seen_ts",
                  "name", "price_low", "price_high"}]}      (the last three: prefetch.py's quick label, once known)

When the stream stops or resets (pause, camera switch, disconnect), what was tracked is purged, and an
"objects" event with no objects says so.

Also {"type": "status", "tracking": "stopped" | "starting" | "ok" | "offline"} when that changes,
and the "look" events pipeline/looks.py publishes as a picked item is identified and priced:

    {"type": "look", "stage": "detected" | "nothing" | "described" | "results" | "failed",
     "capture_id", "track_id" (the tracked object, or null for a photo), ...}

A client that connects mid-Look gets that Look's events so far first, marked "replay": true.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from PIL import Image

from storefront.live.scene import Scene
from storefront.live.tracker import Tracker, TrackResult, encode

log = logging.getLogger("storefront.stream")

VIDEO_EDGE = 1024    # the projector page's MJPEG
VIDEO_FPS = 15
VIDEO_QUALITY = 75
QUEUE = 16           # events buffered per subscriber; a slow one loses the oldest


@dataclass
class Frame:
    image: Image.Image
    captured_at: float  # time.time()
    seq: int


class Stream:
    def __init__(self, tracker: Tracker | None = None):
        self.tracker = tracker or Tracker()
        self._frame: Frame | None = None
        self._tracked: Frame | None = None  # the frame self._result describes
        self._result: TrackResult | None = None
        self._new_frame = asyncio.Event()
        self._task: asyncio.Task | None = None  # the tracking loop
        self._subscribers: set[asyncio.Queue] = set()
        self._status = "stopped"  # the last status published
        self._look: list[dict] = []  # the latest Look's events, replayed to new subscribers
        self._jpeg: tuple[int, bytes] | None = None  # (seq, MJPEG frame): encoded once for every viewer
        self.scene = Scene()
        self._epoch = 0  # bumps when what was tracked is purged: answers to frames sent before are dropped
        # Set by app.py (prefetch.py): told about every tracking result and every purge, and adds fields to
        # each object sent out.
        self.on_objects: Callable[[TrackResult, Image.Image], None] | None = None
        self.object_fields: Callable[[int], dict] | None = None
        self.on_purge: Callable[[], None] | None = None

    # ---------- frames in, results out ----------

    def push_frame(self, img: Image.Image):
        """Called by frame sources on the event loop. The newest frame replaces the one before."""
        self._frame = Frame(img, time.time(), self._frame.seq + 1 if self._frame else 0)
        self._new_frame.set()

    def latest_frame(self) -> Image.Image | None:
        return self._frame.image if self._frame else None

    def latest_objects(self) -> TrackResult | None:
        return self._result

    def tracked_frame(self) -> Image.Image | None:
        """The frame latest_objects() describes, so a crop lines up with its boxes."""
        return self._tracked.image if self._tracked else None

    # ---------- tracking loop ----------

    @property
    def running(self) -> bool:
        return self._task is not None

    async def start(self):
        """Start tracking the frames pushed from now on."""
        await self.stop()
        self._task = asyncio.create_task(self._track())
        log.info("stream: started")
        self._publish_status()

    async def stop(self):
        """Stop the tracking loop, delete the session, and purge what was tracked: scene memory and the
        latest frame. Never raises."""
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            log.info("stream: stopped")
        self._task = None
        await self.tracker.stop()
        self._purge()
        self._publish_status()

    async def reset(self):
        """Forget what was tracked, for a new video source (the phone switched cameras) while tracking
        goes on: scene memory and the latest frame are purged, answers to frames sent before are dropped,
        and the next frame starts a new tracking session, so ids restart. Never raises."""
        self._purge()
        await self.tracker.restart()
        log.info("stream: reset")

    def _purge(self):
        had = self._result is not None or bool(self.scene.tracks)
        self._epoch += 1
        self.scene.clear()
        self._result = self._tracked = self._frame = self._jpeg = None
        if self.on_purge:
            try:
                self.on_purge()
            except Exception:  # a purge never fails
                log.exception("stream: on_purge failed")
        if had:  # subscribers drop their boxes and memory too
            self.publish({"type": "objects", "frame_idx": 0, "width": 0, "height": 0, "age_ms": 0, "objects": []})

    async def _track(self):
        await self.tracker.start()
        self._publish_status()
        while True:
            await self._new_frame.wait()
            self._new_frame.clear()
            frame, epoch = self._frame, self._epoch
            if frame is None:  # purged since it arrived
                continue
            try:
                result = await self.tracker.send(frame.image, frame.captured_at)
            except Exception:  # send() never raises; if a bug makes it, tracking still goes on
                log.exception("stream: tracking failed")
                await asyncio.sleep(1)
                continue
            if result and epoch == self._epoch:
                self._result, self._tracked = result, frame
                self.scene.update(result, frame.image)
                if self.on_objects:
                    try:
                        self.on_objects(result, frame.image)
                    except Exception:  # labels and prefetching must never stop tracking
                        log.exception("stream: on_objects failed")
                self.publish(self._objects_event())
            self._publish_status()

    # ---------- events ----------

    @property
    def tracking(self) -> str:
        if self._task is None:
            return "stopped"
        if self.tracker.error:
            return "offline"
        return "ok" if self._result else "starting"

    def _status_event(self) -> dict:
        return {"type": "status", "tracking": self.tracking}

    def _objects_event(self) -> dict:
        r = self._result
        objects = self.scene.objects()
        if self.object_fields:
            objects = [o | self.object_fields(o["id"]) for o in objects]
        return {
            "type": "objects", "frame_idx": r.frame_idx, "width": r.width, "height": r.height,
            "age_ms": round((time.time() - r.captured_at) * 1000),  # how old the frame these boxes describe is
            "objects": objects,
        }

    def _publish_status(self):
        if self.tracking != self._status:
            self._status = self.tracking
            self.publish(self._status_event())

    def publish(self, event: dict):
        """Send an event to every subscriber; a slow one loses its oldest."""
        if event.get("type") == "look":
            if event["stage"] in ("detected", "nothing"):  # a new Look replaces the last one
                self._look = [event]
            elif self._look and self._look[0]["capture_id"] == event["capture_id"]:
                self._look.append(event)
        for q in self._subscribers:
            if q.full():
                q.get_nowait()
            q.put_nowait(event)

    async def subscribe(self, replay_look: bool = True) -> AsyncIterator[dict]:
        """Events for one client, starting with the current state: the latest Look's events too, unless
        replay_look is False."""
        q: asyncio.Queue = asyncio.Queue(QUEUE)
        self._subscribers.add(q)
        # taken with the queue added: each later Look event goes to the queue, once
        look = list(self._look) if replay_look else []
        try:
            yield self._status_event()
            if self._result:
                yield self._objects_event()
            for event in look:
                yield {**event, "replay": True}  # happened before this client subscribed
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)

    # ---------- video out ----------

    async def video_jpeg(self, frame: Frame) -> bytes:
        if not self._jpeg or self._jpeg[0] != frame.seq:
            self._jpeg = (frame.seq, await asyncio.to_thread(encode, frame.image, VIDEO_EDGE, VIDEO_QUALITY))
        return self._jpeg[1]

    async def mjpeg(self) -> AsyncIterator[bytes]:
        """multipart/x-mixed-replace parts of the newest frames, at most VIDEO_FPS a second."""
        sent = -1
        while True:
            t0 = time.monotonic()
            frame = self._frame
            if frame and frame.seq != sent:
                sent = frame.seq
                jpeg = await self.video_jpeg(frame)
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%b\r\n" % (len(jpeg), jpeg)
            await asyncio.sleep(max(0.0, 1 / VIDEO_FPS - (time.monotonic() - t0)))


live = Stream()
