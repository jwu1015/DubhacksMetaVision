"""Live view: frames in, tracked objects out, as messages to the phone and the projector page.

The phone's video only calls live.push_frame(img). The tracking loop sends the newest frame to the
segmentation server, waits for the answer and repeats, so frames that arrive meanwhile are skipped, never
queued, and the video never waits on tracking. Each answer goes into scene memory (scene.py), to the
pipeline (on_result) and out to every subscriber as an "objects" message (models.ObjectsMessage). A
"status" message goes out when tracking, the category or the prompts change, and the pipeline publishes
"label" and "look" messages through publish().

When the stream stops or resets (pause, camera switch, category change, disconnect), what was tracked is
purged, and an "objects" message with no objects says so. A new subscriber gets the current state first:
status, the latest objects, every label, and each Look's latest message.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from PIL import Image

from storefront.live.scene import Scene
from storefront.live.tracker import Tracker, TrackResult, encode
from storefront.models import ObjectsMessage, StatusMessage

log = logging.getLogger("storefront.stream")

VIDEO_EDGE = 1024    # the projector page's MJPEG
VIDEO_FPS = 15
VIDEO_QUALITY = 75
QUEUE = 64           # messages buffered per subscriber; a slow one loses the oldest


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
        self._labels: dict[str, dict] = {}  # uid -> its "label" message, for new subscribers
        self._looks: dict[str, dict] = {}   # uid -> its Look's latest "look" message, for new subscribers
        self._jpeg: tuple[int, bytes] | None = None  # (seq, MJPEG frame): encoded once for every viewer
        self.scene = Scene()
        self._epoch = 0  # bumps when what was tracked is purged: answers to frames sent before are dropped
        # Set by app.py (the pipeline): told about every tracking result and every purge.
        self.on_result: Callable[[TrackResult, Image.Image], None] | None = None
        self.on_purge: Callable[[], None] | None = None
        self._status = self._status_message()  # the last status sent (or the first a subscriber gets)

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
        and the next frame starts a new tracking session, with new uids. Never raises."""
        self._purge()
        await self.tracker.restart()
        log.info("stream: reset")

    async def set_category(self, category: str):
        """Track things in another category: the next frame starts a session with prompts picked within it."""
        self.tracker.category = category
        await self.reset()
        self._publish_status()

    def _purge(self):
        had = self._result is not None or bool(self.scene.tracks)
        self._epoch += 1
        self.scene.clear()
        self._result = self._tracked = self._frame = self._jpeg = None
        self._labels.clear()
        self._looks.clear()
        if self.on_purge:
            try:
                self.on_purge()
            except Exception:  # a purge never fails
                log.exception("stream: on_purge failed")
        if had:  # subscribers drop their boxes and memory too
            self.publish(ObjectsMessage(session=self.tracker.sessions, age_ms=0, objects=[]).model_dump(mode="json"))

    async def _track(self):
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
                if self.on_result:
                    try:
                        self.on_result(result, frame.image)
                    except Exception:  # the pipeline must never stop tracking
                        log.exception("stream: on_result failed")
                self.publish(self._objects_message())
            self._publish_status()

    # ---------- events ----------

    @property
    def tracking(self) -> str:
        if self._task is None:
            return "stopped"
        if self.tracker.error:
            return "offline"
        return "ok" if self._result else "starting"

    def _status_message(self) -> dict:
        return StatusMessage(tracking=self.tracking, category=self.tracker.category,
                             prompts=self.tracker.prompts).model_dump(mode="json")

    def _objects_message(self) -> dict:
        age_ms = round((time.time() - self._result.captured_at) * 1000)  # how old the frame behind the boxes is
        return ObjectsMessage(session=self.tracker.sessions, age_ms=age_ms,
                              objects=self.scene.objects()).model_dump(mode="json")

    def _publish_status(self):
        status = self._status_message()
        if status != self._status:
            self._status = status
            self.publish(status)

    def publish(self, message: dict):
        """Send a message to every subscriber; a slow one loses its oldest."""
        if message["type"] == "label":
            self._labels[message["uid"]] = message
        elif message["type"] == "look":
            self._looks[message["uid"]] = message
        for q in self._subscribers:
            if q.full():
                q.get_nowait()
            q.put_nowait(message)

    def snapshot(self) -> list[dict]:
        """The current state, as the messages a new subscriber gets first."""
        objects = [self._objects_message()] if self._result else []
        return [self._status_message(), *objects, *self._labels.values(), *self._looks.values()]

    async def subscribe(self) -> AsyncIterator[dict]:
        """Messages for one client, starting with the current state."""
        q: asyncio.Queue = asyncio.Queue(QUEUE)
        self._subscribers.add(q)
        state = self.snapshot()  # taken with the queue added: each later message goes to the queue, once
        try:
            for message in state:
                yield message
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
