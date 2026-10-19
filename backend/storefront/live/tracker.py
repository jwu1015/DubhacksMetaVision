"""Live tracking on Baptiste's SAM server (backend/segmentation/server.py, the /session routes).

One Tracker per stream: one session, one frame in flight. The caller always passes the newest
frame (Stream does this), so frames that arrive meanwhile are skipped, never queued. Each object gets
a uid, "<session number>.<SAM id>", that stays the same for the life of a session; when the server
expires it (404) a new one starts, with new uids. send() never raises: on failure it waits out a
backoff and returns None.

What a session tracks: the prompts Claude picks (labels.py) within the tracker's category from the
session's first frame. The session isn't created until those prompts are back, and that frame is then
its first. A security check (security.py) runs on the same frame at the same time: if recording isn't OK
there, no session starts and the tracker stays blocked (self.blocked, the reason) until stop().

    python -m storefront.live.tracker fixtures/lamp_room.jpg [more.jpg ...]   # one session, then delete it
"""
import asyncio
import io
import logging
import sys
import time

import httpx
from PIL import Image
from pydantic import BaseModel

from storefront import config, net
from storefront.live import labels, security
from storefront.models import BBox

log = logging.getLogger("storefront.tracker")

EDGE = 640           # frames go out 640px wide: the server resizes anyway, bigger only costs upload time
QUALITY = 80
TIMEOUT_S = 10.0     # a busy frame takes ~5s
BACKOFF_S = 1.0      # server busy or down: 1s, 2s, 4s ... up to BACKOFF_MAX_S
BACKOFF_MAX_S = 10.0
AUTH_RETRY_S = 30.0  # token rejected: retrying sooner won't help


class TrackedObject(BaseModel):
    uid: str         # "<session number>.<SAM id>": stable for the life of the session
    label: str       # SAM's prompt that matched, e.g. "lamp"
    score: float
    box: BBox        # normalized 0-1 of the frame


class TrackResult(BaseModel):
    frame_idx: int
    width: int       # the JPEG that was sent, in pixels
    height: int
    objects: list[TrackedObject]
    latency_ms: int
    captured_at: float  # time.time() when the frame was captured


class Blocked(Exception):
    """The security check says recording isn't OK here; the message is its reason."""


class Rejected(Exception):
    """The server won't take our requests (bad or missing token, no URL): logged once, retried slowly."""


def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


def encode(image: Image.Image, width: int = EDGE, quality: int = QUALITY) -> bytes:
    """The image as a JPEG at most `width` px wide (never upscaled)."""
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.Resampling.BILINEAR)
    if image.mode != "RGB":
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def parse(data: dict, latency_ms: int, captured_at: float, session: int = 0) -> TrackResult:
    """The server's reply, with boxes ([x1, y1, x2, y2] in pixels of the JPEG sent) normalized to 0-1."""
    W, H = data["width"], data["height"]
    objects = []
    for o in data["objects"]:
        x1, y1, x2, y2 = (min(max(v / d, 0.0), 1.0) for v, d in zip(o["box"], (W, H, W, H)))
        if x2 > x1 and y2 > y1:
            box = BBox(x=round(x1, 4), y=round(y1, 4), w=round(x2 - x1, 4), h=round(y2 - y1, 4))
            objects.append(TrackedObject(uid=f"{session}.{o['id']}", label=o.get("prompt") or "object",
                                         score=o["score"], box=box))
    return TrackResult(frame_idx=data["frame_idx"], width=W, height=H, objects=objects,
                       latency_ms=latency_ms, captured_at=captured_at)


def describe(e: Exception) -> str:
    return f"{type(e).__name__}: {' '.join(str(e).split())[:200]}".rstrip(": ")


class Tracker:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None, sleep=asyncio.sleep):
        self.base_url = (config.MODEL_SERVICE_URL if base_url is None else base_url).rstrip("/")
        self.client = client  # None: the shared net.client()
        self.sleep = sleep
        self.category = config.DEFAULT_CATEGORY  # the next session's prompts are picked within it
        self.prompts: list[str] = []  # what the current (or last) session tracks
        self.session_id: str | None = None
        self.sessions = 0  # sessions started so far: the current one's number, in its uids
        self.error: str | None = None  # the last failure; None once a frame goes through
        self.blocked: str | None = None  # why recording isn't OK here; no frames go out until stop()
        self.failures = 0
        self._lock = asyncio.Lock()    # frames of a session must never overlap

    async def _request(self, method: str, path: str, headers: dict | None = None, **kw) -> httpx.Response:
        """One call to the server. Raises Rejected on an auth failure; other statuses are the caller's."""
        if not self.base_url:
            raise Rejected("MODEL_SERVICE_URL is unset")
        # wait_for-style hard cap: httpx's timeout only bounds each wait for data.
        # No redirects: without a valid token Cloudflare redirects to a login page.
        async with asyncio.timeout(TIMEOUT_S):
            r = await (self.client or net.client()).request(
                method, f"{self.base_url}{path}", headers={**(headers or {}), **config.segmentation_headers()},
                timeout=TIMEOUT_S, follow_redirects=False, **kw)
        if r.is_redirect or r.status_code in (401, 403):
            raise Rejected(f"Cloudflare Access rejected the request ({r.status_code}); check "
                           "CF_ACCESS_CLIENT_ID/SECRET and the server's access policy")
        return r

    async def _create(self, image: Image.Image):
        """Start a session tracking the prompts Claude picks from `image` (the session's first frame)
        within our category: the session starts only once they are back. Raises Blocked if the security
        check, run at the same time, says recording isn't OK here."""
        prompts, verdict = await asyncio.gather(labels.labels_or_fallback(image, self.category),
                                                security.check(image))
        if not verdict.allowed:
            raise Blocked(verdict.reason)
        r = await self._request("POST", "/session", json={"prompt": prompts})
        r.raise_for_status()
        self.session_id, self.prompts = r.json()["session_id"], prompts
        self.sessions += 1
        log.info("tracker: session %s (#%d) started for %s", self.session_id[:8], self.sessions, prompts)

    async def _frame_request(self, jpeg: bytes) -> httpx.Response:
        try:
            return await self._request("POST", f"/session/{self.session_id}/frame", content=jpeg,
                                       headers={"Content-Type": "image/jpeg"})
        except (TimeoutError, httpx.TimeoutException):
            # The server may still be working on that frame: only a new session can't overlap it.
            await self.stop()
            raise

    async def _post_frame(self, jpeg: bytes, image: Image.Image) -> httpx.Response:
        if not self.session_id:
            await self._create(image)
        r = await self._frame_request(jpeg)
        if r.status_code == 404:  # expired (120s idle) or the server restarted
            log.info("tracker: session %s expired; starting a new one (ids restart)", self.session_id[:8])
            self.session_id = None
            await self._create(image)
            r = await self._frame_request(jpeg)
            if r.status_code == 404:
                self.session_id = None
        r.raise_for_status()
        return r

    async def send(self, image: Image.Image, captured_at: float | None = None) -> TrackResult | None:
        """Track one frame; None if that failed (after waiting out the backoff). Never raises."""
        captured_at = captured_at or time.time()
        async with self._lock:
            if self.blocked:
                return None
            t0 = time.perf_counter()
            try:
                jpeg = await asyncio.to_thread(encode, image)
                r = await self._post_frame(jpeg, image)
                result = parse(r.json(), ms_since(t0), captured_at, self.sessions)
            except Blocked as e:
                log.warning("tracker: blocked: %s", e)
                self.blocked = str(e)
                return None
            except Exception as e:  # rejected, unreachable, slow, error status, unexpected JSON
                await self.sleep(self._note(e, ms_since(t0)))
                return None
        if self.error:
            log.info("tracker: back after %d failures", self.failures)
        self.error, self.failures = None, 0
        log.info("tracker: frame %d %dms objects=%d", result.frame_idx, result.latency_ms, len(result.objects))
        return result

    def _note(self, e: Exception, ms: int = 0) -> float:
        """Log the failure and return how long to wait before the next try."""
        if isinstance(e, Rejected):
            if self.error != str(e):  # once, not every 30s
                log.warning("tracker: %s; retrying every %ds", e, AUTH_RETRY_S)
            self.error = str(e)
            return AUTH_RETRY_S
        delay = min(BACKOFF_S * 2 ** self.failures, BACKOFF_MAX_S)
        self.failures += 1
        self.error = describe(e)
        log.warning("tracker: failed after %dms (%s); retrying in %ds", ms, self.error, delay)
        return delay

    async def restart(self):
        """End the session once the frame in flight (if any) is answered; the next frame starts a new
        session, so ids restart. Never raises."""
        async with self._lock:
            await self.stop()

    async def stop(self):
        """Delete the session, best effort, and lift a block: the next session is checked again. Never raises."""
        self.blocked = None
        sid, self.session_id = self.session_id, None
        if not sid:
            return
        try:
            r = await self._request("DELETE", f"/session/{sid}")
            log.info("tracker: session %s deleted (%d)", sid[:8], r.status_code)
        except Exception as e:
            log.warning("tracker: deleting session %s failed (%s); it expires after 120s idle", sid[:8], describe(e))


async def _cli(paths: list[str]):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tracker = Tracker(sleep=lambda s: asyncio.sleep(0))
    try:
        for path in paths:
            result = await tracker.send(Image.open(path).convert("RGB"))
            print(path, result.model_dump_json(indent=2) if result else f"failed: {tracker.error}")
    finally:
        await tracker.stop()
        await net.close()


if __name__ == "__main__":
    asyncio.run(_cli(sys.argv[1:]))
