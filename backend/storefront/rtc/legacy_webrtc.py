"""WebRTC (legacy, until the app moves to LiveKit): the phone streams the glasses' live video in, and gets
the tracked objects back in the format the current app reads.

    GET  /ice-servers  -> {"iceServers": [...]}    STUN/TURN servers; the phone and this server use the same
    POST /offer        {"sdp", "type": "offer"} -> {"sdp", "type": "answer"}

The phone's offer has one video track (sendonly, H.264 or VP8) and a data channel the phone creates,
"detections". Each video frame goes to stream.live.push_frame() for tracking (the projector page only
shows the LiveKit room's video). Each "objects" message from stream.live goes back on the channel as

    {"type": "detections", "ts": <ms since epoch>,
     "objects": [{"track_id": "3.17", "box": [x1, y1, x2, y2], "label": "lamp", "score": 0.93, "in_view": true,
                  "name": "brass desk lamp", "price_low": 40, "price_high": 90}]}   (box normalized 0-1)

listing every object scene memory holds (scene.py): those that left the view in the last 15 s too, with
in_view false. track_id is the object's uid; name, price_low and price_high are its quick label, once it
has one. The phone's control messages on the same channel:

    {"type": "mode", "mode": "glasses" | "phone"}   on connect and on every camera switch: forget what was tracked
    {"type": "pause", "paused": true | false}       stop tracking and purge it / start again

When the user taps a hotspot the phone sends {"type": "select", "track_id": "3.17"}, which calls
select_handler("3.17"): the pipeline's select. That object's Look then comes back on the channel, its
steps so far at once:

    {"type": "look", "stage": "detected" | "described" | "results" | "failed", "capture_id", "track_id",
     ...}   ("detected": label, quick_phrase, box, cutout_path; "described": item; "results": search,
            the whole search answer; "failed": message)

One phone at a time: a new offer closes the previous connection.

TURN: the phone and the laptop are on different networks, so media relays through Cloudflare's TURN
service. Credentials are minted with our TURN key (CF_TURN_KEY_ID, CF_TURN_KEY_API_TOKEN) and cached;
without a key, static TURN_URLS (comma-separated), TURN_USERNAME and TURN_CREDENTIAL; without those,
STUN only.
"""
import asyncio
import itertools
import json
import logging
import os
import time
from typing import Callable

import httpx
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError
from aiortc.rtcicetransport import parse_stun_turn_uri
from aiortc.sdp import SessionDescription as ParsedSDP
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storefront import config, net
from storefront.live import stream

log = logging.getLogger("storefront.webrtc")
logging.getLogger("aioice").setLevel(logging.WARNING)  # it logs every ICE connectivity check at INFO

CF_TURN_KEY_ID = os.getenv("CF_TURN_KEY_ID", "")
CF_TURN_KEY_API_TOKEN = os.getenv("CF_TURN_KEY_API_TOKEN", "")
TURN_URLS = [u.strip() for u in os.getenv("TURN_URLS", "").split(",") if u.strip()]
TURN_USERNAME = os.getenv("TURN_USERNAME", "")
TURN_CREDENTIAL = os.getenv("TURN_CREDENTIAL", "")
# Frames arrive upright when the phone sets each frame's rotation (we don't negotiate the orientation
# header, so the phone's encoder applies it). If they still arrive sideways: degrees counterclockwise
# to turn them upright (90, 180, 270), as PIL's rotate. Boxes then match the upright view too.
ROTATE = int(os.getenv("WEBRTC_ROTATE", "0") or 0)

CF_TURN_API = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
TURN_TTL_S = 3 * 3600      # Cloudflare allows up to 48h; short, since /ice-servers is public
TURN_MIN_LEFT_S = 3600     # mint anew below this: Cloudflare cuts a call when its credential expires
MINT_TIMEOUT_S = 5.0
MINT_RETRY_S = 30.0        # after a failed mint, don't ask Cloudflare again for this long
STUN = {"urls": ["stun:stun.cloudflare.com:3478"]}
MAX_BUFFERED = 64 * 1024   # bytes waiting on the channel; past this the phone is behind, so skip a message
FPS_LOG_S = 10.0           # log the incoming frame rate this often
SLOW_ANSWER_MS = 3000      # aioice gives up on an unreachable STUN/TURN server after 5s

# Set by app.py: called with the object's uid when the user taps a hotspot.
select_handler: Callable[[str], object] | None = None


def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


# ---------- ICE servers ----------

def usable(servers: list[dict]) -> list[dict]:
    """The servers with only the URLs aiortc can parse, minus port 53: browsers block it, and without
    trickle ICE (aiortc, and the phone's one-shot /offer) a dead URL stalls gathering until it times out."""
    out = []
    for server in servers:
        urls = [server["urls"]] if isinstance(server["urls"], str) else server["urls"]
        keep = []
        for url in urls:
            try:
                if parse_stun_turn_uri(url)["port"] != 53:
                    keep.append(url)
            except ValueError:
                log.warning("webrtc: ignoring malformed ICE server URL %r", url)  # URLs hold no credentials
        if keep:
            out.append({**server, "urls": keep})
    return out


def has_turn(servers: list[dict]) -> bool:
    return any(url.startswith("turn") for s in servers for url in s["urls"])


class MintError(Exception):
    """A reply from Cloudflare we can't use; the message never holds the token or credentials."""


class IceServers:
    """The STUN/TURN servers for both peers: Cloudflare credentials minted with our TURN key and cached
    until an hour before they expire, else the static TURN_* settings, else STUN only. get() never raises."""

    def __init__(self, client: httpx.AsyncClient | None = None, clock: Callable[[], float] = time.time):
        self.client = client  # None: the shared net.client()
        self.clock = clock
        self._minted: list[dict] | None = None
        self._expires = 0.0   # when the minted credential expires
        self._retry_at = 0.0  # no new mint before this, after a failure
        self._static: list[dict] | None = None
        self._lock = asyncio.Lock()  # requests that arrive together share one mint
        self._warned = False

    @property
    def mode(self) -> str:
        if CF_TURN_KEY_ID and CF_TURN_KEY_API_TOKEN:
            return "Cloudflare TURN"
        return "static TURN" if TURN_URLS else "STUN only"

    async def get(self) -> list[dict]:
        if CF_TURN_KEY_ID and CF_TURN_KEY_API_TOKEN:
            async with self._lock:
                now = self.clock()
                if self._expires - now < TURN_MIN_LEFT_S and now >= self._retry_at:
                    await self._mint()
                if self._minted and self.clock() < self._expires:  # after a failed refresh: the old one
                    return self._minted
        if TURN_URLS:
            if self._static is None:
                self._static = usable([STUN, {"urls": TURN_URLS, "username": TURN_USERNAME,
                                              "credential": TURN_CREDENTIAL}])
            if has_turn(self._static):
                return self._static
        if not self._warned:
            self._warned = True
            why = "minting failed" if CF_TURN_KEY_ID and CF_TURN_KEY_API_TOKEN else \
                "set CF_TURN_KEY_ID and CF_TURN_KEY_API_TOKEN"
            log.warning("webrtc: no TURN server (%s), STUN only: a phone on another network than this "
                        "laptop may not connect", why)
        return [STUN]

    async def _mint(self):
        t0, minted_at = time.perf_counter(), self.clock()
        try:
            async with asyncio.timeout(MINT_TIMEOUT_S):  # httpx's timeout only bounds each wait for data
                r = await (self.client or net.client()).post(
                    CF_TURN_API.format(key_id=CF_TURN_KEY_ID), json={"ttl": TURN_TTL_S},
                    headers={"Authorization": f"Bearer {CF_TURN_KEY_API_TOKEN}"},
                    timeout=MINT_TIMEOUT_S, follow_redirects=False)
            if not r.is_success:
                raise MintError(f"HTTP {r.status_code}")
            servers = usable(r.json()["iceServers"])
            if not has_turn(servers):
                raise MintError("no TURN server in the reply")
        except Exception as e:  # unreachable, slow, rejected, unexpected JSON
            self._retry_at = self.clock() + MINT_RETRY_S
            # Only our own message or the exception's type: never the token, never the reply.
            log.warning("webrtc: minting TURN credentials failed (%s) after %dms; retrying in %ds",
                        e if isinstance(e, MintError) else type(e).__name__, ms_since(t0), MINT_RETRY_S)
            return
        self._minted, self._expires = servers, minted_at + TURN_TTL_S
        log.info("webrtc: TURN credentials minted in %dms, valid for %dh", ms_since(t0), TURN_TTL_S // 3600)


def rtc_configuration(servers: list[dict]) -> RTCConfiguration:
    # Never iceServers=None: aiortc would add Google's STUN server.
    return RTCConfiguration(iceServers=[
        RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential")) for s in servers])


# ---------- channel messages ----------

def detections(objects: dict, labels: dict[str, dict]) -> dict:
    """An "objects" message as the app's DetectionsMessage, each object with its quick label once it has one.
    Those out of view (in_view false) aren't hotspots, but a voice command can name them."""
    out = []
    for o in objects["objects"]:
        d = {"track_id": o["uid"], "box": o["box"], "label": o["prompt"], "score": o["score"], "in_view": o["in_view"]}
        if label := labels.get(o["uid"]):
            d |= {k: label[k] for k in ("name", "price_low", "price_high")}
        out.append(d)
    return {"type": "detections", "ts": round(time.time() * 1000), "objects": out}


def look_steps(look: dict, sent: set[str]) -> list[dict]:
    """A picked object's "look" message as the app's Look steps it hasn't had yet (sent): "detected" first,
    then "described", then "results" or "failed"."""
    base = {"type": "look", "capture_id": look["look_id"], "track_id": look["uid"]}
    x1, y1, x2, y2 = look["box"]
    label = look["label"]
    steps = []
    if "detected" not in sent:
        cutout = (look["crop_url"] or "").removeprefix(config.PUBLIC_BASE_URL + "/")
        steps.append(base | {"stage": "detected", "label": label, "quick_phrase": f"{label.capitalize()}. Finding prices.",
                             "box": {"x": x1, "y": y1, "w": round(x2 - x1, 4), "h": round(y2 - y1, 4)},
                             "cutout_path": cutout})
    if look["item"] and "described" not in sent:
        steps.append(base | {"stage": "described", "item": look["item"]})
    if look["stage"] == "results" and "results" not in sent:
        steps.append(base | {"stage": "results", "search": look["search"]})
    if look["stage"] == "failed" and "failed" not in sent:
        steps.append(base | {"stage": "failed", "message": look["message"]})
    sent.update(step["stage"] for step in steps)
    return steps


def describe(sdp: str) -> str:
    """What an answer accepted, e.g. "video recvonly H264, data channel"."""
    try:
        parts = []
        for m in ParsedSDP.parse(sdp).media:
            if m.kind == "application":
                parts.append("data channel")
            else:
                codec = m.rtp.codecs[0].mimeType.split("/")[-1] if m.rtp.codecs else "no codec"
                parts.append(f"{m.kind} {m.direction} {codec}")
        return ", ".join(parts) or "no media"
    except Exception:
        return "unparsed"


def route(pc: RTCPeerConnection) -> str:
    """The candidate pair in use, local -> remote, e.g. "relay -> srflx" (relay = through TURN)."""
    try:  # aioice internals: only for the log
        ice = pc.getTransceivers()[0].receiver.transport.transport._connection
        pair = ice._nominated[1]
        return f"{pair.local_candidate.type} -> {pair.remote_candidate.type}"
    except Exception:
        return "unknown"


# ---------- one phone ----------

_ids = itertools.count(1)


class Phone:
    """One phone's peer connection: video in to stream.live, detections out, selects in."""

    def __init__(self, pc: RTCPeerConnection, phones: "Phones"):
        self.pc, self.phones, self.id = pc, phones, next(_ids)
        self.t0 = time.perf_counter()
        self.closed = False
        self.started = False  # this connection started stream.live
        self.paused = False   # the user paused the camera: no tracking, frames dropped
        self.mode: str | None = None  # "glasses" or "phone", as the phone last said
        self.frames = self.sent = self.skipped = 0
        self.size: tuple[int, int] | None = None
        self.labels: dict[str, dict] = {}         # uid -> its "label" message
        self.picked: dict[str, set[str]] = {}     # uid this phone selected -> the Look steps it was sent
        self._tasks: set[asyncio.Task] = set()
        pc.on("signalingstatechange", lambda: self._log("signaling", pc.signalingState))
        pc.on("icegatheringstatechange", lambda: self._log("ICE gathering", pc.iceGatheringState))
        pc.on("iceconnectionstatechange", lambda: self._log("ICE", pc.iceConnectionState))
        pc.on("connectionstatechange", self._on_state)
        pc.on("track", self._on_track)
        pc.on("datachannel", self._on_channel)

    def __str__(self):
        return f"phone {self.id}"

    def _log(self, what: str, state: str):
        log.info("webrtc: %s %s %s (%dms)", self, what, state, ms_since(self.t0))

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _on_state(self):
        state = self.pc.connectionState
        self._log("connection", state + (f" via {route(self.pc)}" if state == "connected" else ""))
        if state == "connected":
            await self.phones.connected(self)
        elif state in ("failed", "closed"):
            await self.phones.ended(self)

    # ---------- video in ----------

    def _on_track(self, track):
        log.info("webrtc: %s sends %s", self, track.kind)
        if track.kind != "video":
            log.warning("webrtc: %s sent a %s track; only video is used", self, track.kind)
        self._spawn(self._receive(track))  # other kinds are read too, or their frames would pile up

    async def _receive(self, track):
        counted, since = 0, None
        try:
            while True:
                frame = await track.recv()
                if track.kind != "video":
                    continue
                self._push(frame)
                now = time.monotonic()
                since, counted = since or now, counted + 1
                if now - since >= FPS_LOG_S:
                    log.info("webrtc: %s video %.1f fps %dx%d, %d detections sent", self,
                             counted / (now - since), *self.size, self.sent)
                    counted, since = 0, now
        except MediaStreamError:
            log.info("webrtc: %s %s track ended after %d frames", self, track.kind, self.frames)

    def _push(self, frame):
        if self.paused:  # nothing is kept while the camera is paused
            return
        # Under 1ms for 504x896, so it stays on the loop: frames never queue up behind it.
        img = frame.to_image()
        if ROTATE:
            img = img.rotate(ROTATE, expand=True)
        if img.size != self.size:
            log.info("webrtc: %s video %dx%d%s", self, *img.size, f", rotated {ROTATE}" if ROTATE else "")
            self.size = img.size
        if not self.frames:
            self._log("first frame", "in")
        self.frames += 1
        stream.live.push_frame(img)

    # ---------- the detections channel ----------

    def _on_channel(self, channel):
        log.info("webrtc: %s channel %r %s (%dms)", self, channel.label, channel.readyState, ms_since(self.t0))
        if channel.label != "detections":
            log.warning("webrtc: %s opened channel %r; only \"detections\" is used", self, channel.label)
            return
        sender = self._spawn(self._send(channel))
        channel.on("message", self._on_message)

        @channel.on("close")
        def closed():
            log.info("webrtc: %s channel %r closed", self, channel.label)
            sender.cancel()

    async def _send(self, channel):
        """Every "objects" message as a detections message, and the steps of each Look this phone picked.
        send() only queues, so a slow phone never holds up the stream; one that falls far behind misses
        detections instead (never a Look step)."""
        messages = stream.live.subscribe()
        try:
            async for m in messages:
                if channel.readyState != "open":
                    return
                if m["type"] == "label":
                    self.labels[m["uid"]] = m
                elif m["type"] == "look" and m["selected"] and m["uid"] in self.picked:
                    for step in look_steps(m, self.picked[m["uid"]]):
                        channel.send(json.dumps(step))
                        log.info("webrtc: %s look %s sent for %s", self, step["stage"], m["uid"])
                elif m["type"] == "objects":
                    if not m["objects"]:  # tracking restarted: uids start over
                        self.labels.clear()
                    if channel.bufferedAmount > MAX_BUFFERED:
                        self.skipped += 1
                        continue
                    channel.send(json.dumps(detections(m, self.labels)))
                    if not self.sent:
                        self._log("first detections", f"sent ({len(m['objects'])} objects)")
                    self.sent += 1
        finally:
            await messages.aclose()

    def _on_message(self, message):
        try:
            msg = json.loads(message)
        except ValueError:
            msg = None
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "mode":
            self._on_mode(msg.get("mode"))
        elif kind == "pause":
            self._on_pause(msg.get("paused"))
        elif kind == "select":
            self._on_select(msg.get("track_id"))
        else:
            log.info("webrtc: %s sent a message of type %.40r; ignored", self, kind)

    def _on_mode(self, mode):
        """{"type": "mode", "mode": "glasses" | "phone"}, on connect and on every switch. The phone swaps
        the camera without renegotiating, so what was tracked belongs to the old one: reset."""
        if mode not in ("glasses", "phone"):
            log.info("webrtc: %s sent mode %.40r; ignored", self, mode)
            return
        log.info("webrtc: %s mode %s", self, mode)
        self.mode = mode
        self._spawn(self.phones.reset(self))

    def _on_pause(self, paused):
        """{"type": "pause", "paused": true | false}: the user paused or resumed the camera."""
        if not isinstance(paused, bool):
            log.info("webrtc: %s sent pause %.40r; ignored", self, paused)
            return
        self._spawn(self.phones.pause(self, paused))

    def _on_select(self, uid):
        if not isinstance(uid, str) or not uid:
            log.info("webrtc: %s selected %.40r, which isn't a track_id; ignored", self, uid)
            return
        if select_handler is None:
            log.info("webrtc: %s selected %s; no select handler is set, ignored", self, uid)
            return
        log.info("webrtc: %s selected %s", self, uid)
        self.picked.setdefault(uid, set())
        try:
            select_handler(uid)
        except Exception:
            log.exception("webrtc: %s select %s failed", self, uid)

    # ---------- the end ----------

    async def close(self):
        """Stop reading and sending, and close the peer connection. Never raises."""
        if self.closed:
            return
        self.closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self.pc.close()
        except Exception as e:
            log.warning("webrtc: %s close failed: %r", self, e)
        log.info("webrtc: %s closed after %ds: %d frames in, %d detections out%s", self, ms_since(self.t0) // 1000,
                 self.frames, self.sent, f", {self.skipped} skipped (phone behind)" if self.skipped else "")


class Phones:
    """The phone connection: one at a time, a new offer replaces it. The lock keeps offers, and starting
    and stopping stream.live, in order, so a connection that just ended can't stop the next one's stream."""

    def __init__(self, ice: IceServers):
        self.ice = ice
        self.current: Phone | None = None
        self.lock = asyncio.Lock()

    async def answer(self, offer: RTCSessionDescription) -> RTCSessionDescription:
        """Close the previous phone, then answer this one. ValueError if the offer is unusable."""
        async with self.lock:
            await self._close_current()
            t0 = time.perf_counter()
            pc = RTCPeerConnection(rtc_configuration(await self.ice.get()))
            phone = self.current = Phone(pc, self)
            try:
                await pc.setRemoteDescription(offer)
            except Exception as e:
                await self._close_current()
                raise ValueError(f"unusable offer: {' '.join(str(e).split())[:200] or type(e).__name__}") from e
            if not any(t.kind == "video" for t in pc.getTransceivers()):
                await self._close_current()
                raise ValueError("the offer has no video track")
            try:
                await pc.setLocalDescription(await pc.createAnswer())
            except Exception:
                await self._close_current()
                raise
            answer, ms = pc.localDescription, ms_since(t0)
            log.info("webrtc: %s answered in %dms: %s (ICE: %s)", phone, ms, describe(answer.sdp), self.ice.mode)
            if ms > SLOW_ANSWER_MS:
                log.warning("webrtc: answering took %dms: gathering waited out a network interface that can't "
                            "reach the STUN/TURN server, often a VPN such as Tailscale; quit it to connect faster", ms)
            return answer

    async def connected(self, phone: Phone):
        async with self.lock:
            if phone is self.current and not phone.started:
                phone.started = True
                await stream.live.start()

    async def ended(self, phone: Phone):
        async with self.lock:
            if phone is self.current:
                await self._close_current()

    async def reset(self, phone: Phone):
        """The phone switched cameras: forget what was tracked; tracking goes on with the new one."""
        async with self.lock:
            if phone is self.current and phone.started and not phone.paused:
                await stream.live.reset()

    async def pause(self, phone: Phone, paused: bool):
        """Paused: stop tracking and purge what was tracked (scene memory, the latest frame, the session
        on the tracking server). Resumed: start again."""
        async with self.lock:
            if phone is not self.current or phone.paused == paused:
                return
            phone.paused = paused
            log.info("webrtc: %s camera %s", phone, "paused" if paused else "resumed")
            if phone.started:
                await (stream.live.stop() if paused else stream.live.start())

    async def _close_current(self):
        phone, self.current = self.current, None
        if phone:
            await phone.close()
            if phone.started:
                await stream.live.stop()  # deletes the tracking session

    async def close(self):
        """Close the phone's connection and stop its stream (for shutdown). Never raises."""
        async with self.lock:
            await self._close_current()


phones = Phones(IceServers())


async def close():
    await phones.close()


# ---------- routes ----------

router = APIRouter()


class SessionDescription(BaseModel):  # SessionDescription in the app's Models.swift
    sdp: str
    type: str  # "offer" from the phone, "answer" back


@router.get("/ice-servers")
async def ice_servers_route():
    """Call before connecting; use the list as RTCConfiguration.iceServers as is."""
    return {"iceServers": await phones.ice.get()}


@router.post("/offer", response_model=SessionDescription)
async def offer_route(offer: SessionDescription):
    if offer.type != "offer":
        raise HTTPException(400, 'type must be "offer"')
    try:
        answer = await phones.answer(RTCSessionDescription(sdp=offer.sdp, type="offer"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return SessionDescription(sdp=answer.sdp, type=answer.type)
