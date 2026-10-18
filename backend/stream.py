"""Live view: frames in, tracked boxes out, shown on the projector page.

Frame sources (a video file now, the glasses over WebRTC later) only call live.push_frame(img).
The tracking loop sends the newest frame to Baptiste's server, waits for the answer and repeats,
so frames that arrive meanwhile are skipped, never queued, and the video never waits on tracking.
Each answer goes to the projector page (and any other subscriber) as an "objects" event:

    {"type": "objects", "frame_idx", "width", "height", "age_ms",
     "objects": [{"id", "label", "score", "box": {"x", "y", "w", "h"}}]}     (box normalized 0-1)

plus {"type": "status", "tracking": "stopped" | "starting" | "ok" | "offline"} when that changes,
and the "look" events main.py publishes as a picked item is identified and priced:

    {"type": "look", "stage": "detected" | "nothing" | "described" | "results" | "failed",
     "capture_id", "track_id" (the tracked object, or null for a photo), ...}

A client that connects mid-Look gets that Look's events so far first, marked "replay": true.

    python stream.py --video test_media/room.mp4 [--port 8002]   # then open http://localhost:8002/stream/viewer
    python stream.py --webcam
"""
import argparse
import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

import av
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel

import config
import net
from tracker import Tracker, TrackResult, encode

log = logging.getLogger("gtl.stream")

STATIC = config.ROOT / "static"
MEDIA = config.ROOT / "test_media"  # POST /stream/start only plays files from here
MAX_EDGE = 1920      # bigger source frames are scaled down while decoding
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
        self._tasks: list[asyncio.Task] = []
        self._subscribers: set[asyncio.Queue] = set()
        self._status = "stopped"  # the last status published
        self._look: list[dict] = []  # the latest Look's events, replayed to new subscribers
        self._jpeg: tuple[int, bytes] | None = None  # (seq, MJPEG frame): encoded once for every viewer
        # Set by main.py (prefetch.py): told about every tracking result, and adds fields to each object sent out.
        self.on_objects: Callable[[TrackResult, Image.Image], None] | None = None
        self.object_fields: Callable[[int], dict] | None = None

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
        return bool(self._tasks)

    async def start(self, source: "Source | None" = None):
        """Start tracking, and the source if given (without one, frames come from whoever pushes them)."""
        await self.stop()
        self._tasks = [asyncio.create_task(self._track())]
        if source:
            self._tasks.append(asyncio.create_task(source.run(self)))
        log.info("stream: started, source=%s", source)
        self._publish_status()

    async def stop(self):
        """Stop the source and the tracking loop, then delete the session. Never raises."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._tasks:
            log.info("stream: stopped")
        self._tasks = []
        await self.tracker.stop()
        self._result = self._tracked = None
        self._publish_status()

    async def _track(self):
        await self.tracker.start()
        self._publish_status()
        while True:
            await self._new_frame.wait()
            self._new_frame.clear()
            frame = self._frame
            try:
                result = await self.tracker.send(frame.image, frame.captured_at)
            except Exception:  # send() never raises; if a bug makes it, tracking still goes on
                log.exception("stream: tracking failed")
                await asyncio.sleep(1)
                continue
            if result:
                self._result, self._tracked = result, frame
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
        if not self._tasks:
            return "stopped"
        if self.tracker.error:
            return "offline"
        return "ok" if self._result else "starting"

    def _status_event(self) -> dict:
        return {"type": "status", "tracking": self.tracking}

    def _objects_event(self) -> dict:
        r = self._result
        return {
            "type": "objects", "frame_idx": r.frame_idx, "width": r.width, "height": r.height,
            "age_ms": round((time.time() - r.captured_at) * 1000),  # how old the frame these boxes describe is
            "objects": [{"id": o.id, "label": o.label, "score": o.score, "box": o.box.model_dump(),
                         **(self.object_fields(o.id) if self.object_fields else {})}
                        for o in r.objects],
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


# ---------- frame sources ----------

class Source:
    """Decodes video with PyAV in a worker thread and pushes PIL frames to a Stream."""
    paced = True   # sleep until each frame's timestamp (a file); a live device delivers in real time
    loop = False

    def open(self) -> av.container.InputContainer:
        raise NotImplementedError

    async def run(self, stream: Stream):
        """Push frames until cancelled, or until the video ends (unless looping). Never raises."""
        loop, stop = asyncio.get_running_loop(), threading.Event()

        def push(img: Image.Image):
            loop.call_soon_threadsafe(stream.push_frame, img)
        try:
            while not stop.is_set():
                if not await asyncio.to_thread(self._play, push, stop) or not self.loop:
                    log.info("source %s: ended", self)
                    return
        except Exception as e:
            log.warning("source %s failed: %r", self, e)
        finally:
            stop.set()  # the thread checks this every frame

    def _play(self, push: Callable[[Image.Image], None], stop: threading.Event) -> int:
        """Decode the video once (in a worker thread), pushing each frame; returns the frame count."""
        n, start = 0, None
        with self.open() as container:
            video = container.streams.video[0]
            video.thread_type = "AUTO"
            for frame in container.decode(video):
                if stop.is_set():
                    break
                scale = min(1.0, MAX_EDGE / max(frame.width, frame.height))
                img = frame.to_image(width=round(frame.width * scale), height=round(frame.height * scale))
                if frame.rotation:  # phone videos: degrees counterclockwise to display upright, as PIL's rotate
                    img = img.rotate(frame.rotation, expand=True)
                if self.paced and frame.time is not None:
                    if start is None:
                        start = time.monotonic() - frame.time
                    if stop.wait(max(0.0, start + frame.time - time.monotonic())):
                        break
                push(img)
                n += 1
        return n


class FileSource(Source):
    """A video file at its real frame rate, looping by default."""

    def __init__(self, path: str | Path, loop: bool = True):
        self.path, self.loop = Path(path), loop

    def open(self):
        return av.open(str(self.path))

    def __str__(self):
        return self.path.name


class WebcamSource(Source):
    """The Mac's camera through PyAV's avfoundation input."""
    paced = False

    def __init__(self, device: str = "0"):
        self.device = device

    def open(self):
        return av.open(self.device, format="avfoundation", options={"framerate": "30", "video_size": "1280x720"})

    def __str__(self):
        return f"webcam {self.device}"


def source_from(spec: str) -> Source:
    """"webcam", or "file:<path>" relative to backend/ and inside test_media/."""
    if spec == "webcam":
        return WebcamSource()
    if spec.startswith("file:"):
        path = (config.ROOT / spec.removeprefix("file:")).resolve()
        if path.is_relative_to(MEDIA.resolve()) and path.is_file():
            return FileSource(path)
    raise HTTPException(400, "source must be 'webcam' or 'file:test_media/<video>'")


# ---------- routes ----------

router = APIRouter(prefix="/stream")
live = Stream()


def local_only(request: Request):
    """The test routes are for this laptop only. Tunnels (ngrok, Cloudflare) connect from localhost
    too, but add X-Forwarded-For."""
    if not request.client or request.client.host not in ("127.0.0.1", "::1") or "x-forwarded-for" in request.headers:
        raise HTTPException(403, "local requests only")


class StartRequest(BaseModel):
    source: str  # "file:test_media/room.mp4" or "webcam"


@router.get("/viewer", include_in_schema=False)
async def viewer_route():
    return FileResponse(STATIC / "viewer.html")


@router.get("/video.mjpg")
async def video_route():
    return StreamingResponse(live.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@router.websocket("/events")
async def events_route(ws: WebSocket):
    """The JSON events, starting with the current state, until the client goes away."""
    await ws.accept()

    async def send_all():
        with suppress(Exception):  # the client left mid-send
            async for event in live.subscribe():
                await ws.send_json(event)

    sender = asyncio.create_task(send_all())
    try:
        while (await ws.receive())["type"] != "websocket.disconnect":
            pass
    finally:
        sender.cancel()


@router.post("/start", dependencies=[Depends(local_only)])
async def start_route(req: StartRequest):
    await live.start(source_from(req.source))
    return {"tracking": live.tracking}


@router.post("/stop", dependencies=[Depends(local_only)])
async def stop_route():
    await live.stop()
    return {"tracking": live.tracking}


# ---------- standalone runner ----------

def main():
    p = argparse.ArgumentParser(description="Serve the projector page alone, fed by a video file or the webcam.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--video", help="video file, played in a loop")
    src.add_argument("--webcam", action="store_true")
    p.add_argument("--port", type=int, default=8002)  # 8000 is the main server
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.video and not Path(args.video).is_file():
        p.error(f"no such file: {args.video}")
    source = WebcamSource() if args.webcam else FileSource(args.video) if args.video else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if source:
            await live.start(source)
        log.info("viewer: http://localhost:%d/stream/viewer  tracking: %s", args.port,
                 config.MODEL_SERVICE_URL or "MODEL_SERVICE_URL unset")
        yield
        await live.stop()
        await net.close()

    app = FastAPI(title="Get the Look stream", lifespan=lifespan)
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/stream/viewer")

    # The MJPEG response never ends on its own: give open viewers 1s, then stop (and delete the session).
    uvicorn.run(app, host=args.host, port=args.port, timeout_graceful_shutdown=1)


if __name__ == "__main__":
    main()
