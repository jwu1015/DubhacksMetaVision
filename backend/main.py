"""Routes, static uploads, and the Look on a tracked object.

    uvicorn main:app --reload --port 8000 --timeout-graceful-shutdown 1        (from backend/)

(--timeout-graceful-shutdown 1: an open projector page streams forever, which would otherwise block
Ctrl+C and --reload.)
"""
import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable
from contextlib import asynccontextmanager, contextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from livekit import api as livekit_api

import cart
import config
import crop
import net
import prefetch
import pricing
import rank
import rerank
import search
import stream
import vision
import webrtc
from models import (
    Capture, CheckoutRequest, CheckoutResponse, DetectResponse, HealthResponse, Item, Product,
    SearchResponse, VoiceTokenRequest, VoiceTokenResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gtl")

captures: dict[str, Capture] = {}
# capture -> the live-stream object it looked at, so the projector can highlight that box
capture_tracks: dict[str, int] = {}
search_tasks: dict[str, asyncio.Task] = {}


def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


@contextmanager
def timed(timing: dict[str, int], key: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timing[key] = ms_since(t0)


def fixture(name: str):
    return json.loads((config.FIXTURES / name).read_text().replace("__BASE__", config.PUBLIC_BASE_URL))


async def model_service_ok(timeout: float = 3.0) -> bool:
    if not config.MODEL_SERVICE_URL:
        return False
    try:
        r = await net.client().get(
            f"{config.MODEL_SERVICE_URL}/health", timeout=timeout,
            headers=config.segmentation_headers(), follow_redirects=False,  # no login-page redirects
        )
        body = r.json()
        # Baptiste's SAM server answers {"status": "ok"}; the spec's model service {"ok": true}.
        return r.status_code == 200 and (body.get("ok") is True or body.get("status") == "ok")
    except Exception:  # unreachable, timeout, bad JSON: all just mean "not ok"
        return False


def announce(cap: Capture, step: str, **extra):
    """Tell the projector page (and anyone following the stream) how a Look is going. Steps:
    "detected" (label + crop), "described" (Claude's name), "results" (the cards), "failed"."""
    stream.live.publish({"type": "look", "stage": step, "capture_id": cap.capture_id,
                         "track_id": capture_tracks.get(cap.capture_id), **extra})


# ---------- the Look on a tracked object ----------

async def look(track_id: int) -> DetectResponse | None:
    """The user picked a tracked object (the phone's "select" message, POST /look/{id}, or the voice
    assistant). The crop comes from that track's best frame in scene memory (sharpest, biggest, most
    centered), in view or not, with the box it had there; the projector follows along through the "look"
    events. None if scene memory doesn't hold that track (any more).
    If prefetch.py has looked at the object in the background, this Look takes that one's description and
    results, at once if it's done."""
    best = stream.live.scene.best(track_id)
    if best is None:
        log.info("look: scene memory has no object %d", track_id)
        return None
    t0 = time.perf_counter()
    frame, track = best
    frame = frame.convert("RGB")
    capture_id = "c_" + uuid.uuid4().hex[:8]
    capture_tracks[capture_id] = track_id
    image_path = f"uploads/{capture_id}.jpg"
    frame.save(config.ROOT / image_path, format="JPEG", quality=90)
    found = crop.Found(track.frame_box, track.score, track.label)
    segmentation = crop.sam_crop(frame, found, capture_id, t0)

    label = found.label.strip()
    item = Item(category=label or "item", bbox=found.box, search_queries=[label] if label else [],
                quick_phrase=f"{label[:1].upper()}{label[1:]}. Finding prices." if label else "Got it. Finding prices.")
    cap = captures[capture_id] = Capture(capture_id=capture_id, image_path=image_path,
                                         image_url=f"{config.PUBLIC_BASE_URL}/{image_path}",
                                         item=item, segmentation=segmentation)
    announce(cap, "detected", label=item.category, quick_phrase=item.quick_phrase,
             box=segmentation.bbox.model_dump(), cutout_path=segmentation.cutout_path)

    background = prefetch.claim(track_id)
    if background:
        log.info("look %s: taking the background Look on t%d (%s)", capture_id, track_id,
                 "done" if background.search.done() else "still running")
        # Shielded: this Look ending early mustn't cancel the background one's work.
        claude, searched = asyncio.shield(background.describe), asyncio.shield(background.search)
    else:
        claude = asyncio.create_task(vision.detect((config.ROOT / segmentation.cutout_path).read_bytes()))
        searched = None
    search_tasks[capture_id] = asyncio.create_task(describe_then_search(cap, claude, searched))
    log.info("look %s on t%d: %dms", capture_id, track_id, ms_since(t0))
    return DetectResponse(capture_id=capture_id, image_url=cap.image_url, item=item, segmentation=segmentation,
                          quick_phrase=item.quick_phrase, latency_ms=ms_since(t0))


async def describe_then_search(cap: Capture, claude: Awaitable[Item],
                               searched: Awaitable[SearchResponse] | None = None) -> SearchResponse:
    """Claude describes the crop, then the stores are searched on Claude's queries (a label like "lamp"
    alone finds lamps, not this one). Claude failing leaves the label. searched: a background Look's
    results for the same description, taken instead of searching again."""
    t0 = time.perf_counter()
    label_item = cap.item
    try:
        item = await claude
        if item.category:
            cap.item = item.model_copy(update={"bbox": label_item.bbox, "quick_phrase": label_item.quick_phrase})
            announce(cap, "described", item=cap.item.model_dump(mode="json"))
    except Exception as e:
        log.warning("describe %s: Claude failed (%r); searching with SAM's label", cap.capture_id, e)
    log.info("describe %s: %dms category=%r queries=%s", cap.capture_id, ms_since(t0),
             cap.item.category, cap.item.search_queries)
    try:
        if searched is not None:
            resp = (await searched).model_copy(update={"capture_id": cap.capture_id})
        else:
            resp = await search_item(cap.item, cap.segmentation.cutout_url, cap.capture_id)
    except Exception:
        announce(cap, "failed", message="Couldn't get prices for that one.")
        raise
    resp.item = cap.item
    announce(cap, "results", search=resp.model_dump(mode="json"))
    return resp


async def search_item(item: Item, cutout: str | bytes, capture_id: str = "") -> SearchResponse:
    """search -> rerank -> pricing + rank. cutout: the item's crop, by URL, or the crop itself (a background
    Look keeps it in memory only); capture_id: "" for a background Look, which has no capture."""
    t0 = time.perf_counter()
    timing: dict[str, int] = {}
    with timed(timing, "search"):
        products = await search.search_all(cutout, item, capture_id=capture_id or None)
    with timed(timing, "rerank"):
        products = await rerank.rerank(cutout, products)
    with timed(timing, "rank"):
        resp = await rank.choose(item, products, pricing.compute_price_insight(products))
    resp.capture_id = capture_id
    resp.timing_ms = timing | {"total": ms_since(t0)}
    log.info("search %s products=%d timing=%s", capture_id or "(background)", len(products), resp.timing_ms)
    return resp


def background_look(track_id: int) -> prefetch.Background | None:
    """prefetch.py's start_look: a Look at a tracked object nobody has picked yet, in memory only. Claude
    describes the crop of the object's best frame, then the stores are searched on Claude's queries.
    Nothing is written to disk or announced, and the crop is dropped once both are done; look() takes the
    results if the user picks the object. None if scene memory has no frame of it."""
    best = stream.live.scene.best(track_id)
    if best is None:
        return None
    frame, track = best
    cutout = crop.sam_cutout(frame, track.frame_box)
    label = track.label.strip()
    describe = asyncio.create_task(vision.detect(cutout))

    async def find() -> SearchResponse:
        try:
            item = await describe
        except Exception as e:  # as in describe_then_search: the label is searched instead
            log.warning("background t%d: Claude failed (%r); searching with the label", track_id, e)
            item = None
        if item is None or not item.category:
            item = Item(category=label or "item", search_queries=[label] if label else [])
        return await search_item(item, cutout)

    return prefetch.Background(describe, asyncio.create_task(find()))


async def warm_up():
    """Make the first real request as fast as later ones: one call per Claude stage (vision, rank) so each
    connection is open and the API has prepared its output schema (about a cent in total)."""
    if not config.WARMUP:
        return
    item = Item.model_validate(fixture("item_lamp.json"))
    products = [Product.model_validate(p) for p in fixture("products_ranked_lamp.json")]
    calls = [("vision", vision.detect((config.FIXTURES / "lamp_room.jpg").read_bytes())),
             ("rank", rank.choose(item, products, pricing.compute_price_insight(products)))]
    for name, call in calls:
        t0 = time.perf_counter()
        try:
            await call
            log.info("warm-up: %s call %dms", name, ms_since(t0))
        except Exception as e:
            log.warning("warm-up: %s call failed: %r", name, e)


# ---------- app ----------

config.UPLOADS.mkdir(exist_ok=True)
config.LOGS.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In the background, so the server accepts requests right away: wake the segmentation server
    # (no cold start on the first request), warm up our own stages, and get TURN credentials (the
    # phone's first /ice-servers is then fast).
    background = [asyncio.create_task(model_service_ok(timeout=90.0)), asyncio.create_task(warm_up()),
                  asyncio.create_task(webrtc.phones.ice.get())]
    log.info("ready: warmup=%s ICE=%s base=%s", config.WARMUP, webrtc.phones.ice.mode, config.PUBLIC_BASE_URL)
    yield
    for task in background + list(search_tasks.values()):
        task.cancel()
    await webrtc.close()      # the phone's connection
    await stream.live.stop()  # deletes the tracking session on the segmentation server
    await net.close()


app = FastAPI(title="Get the Look backend", lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=config.UPLOADS), name="uploads")
app.include_router(cart.router)


@app.post("/look/{track_id}", response_model=DetectResponse, dependencies=[Depends(stream.local_only)])
async def look_route(track_id: int):
    """Look at a tracked object from this laptop (the phone does it over WebRTC)."""
    resp = await look(track_id)
    if resp is None:
        raise HTTPException(404, f"the stream isn't tracking object {track_id}")
    return resp


@app.post("/voice/token", response_model=VoiceTokenResponse, status_code=201)
async def voice_token_route(req: VoiceTokenRequest | None = None):
    """The phone's ticket to the voice assistant (voice.py), in LiveKit's standard token-endpoint format,
    so the app can use its SDK's endpoint token source. Every call opens a new room: LiveKit dispatches
    the agent when a room is created."""
    if not (config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET):
        raise HTTPException(503, "voice isn't set up: LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET are unset")
    req = req or VoiceTokenRequest()
    room = "spotted-" + uuid.uuid4().hex[:8]
    token = (livekit_api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
             .with_identity(req.participant_identity or "phone-" + uuid.uuid4().hex[:6])
             .with_name(req.participant_name or "Spotted user")
             .with_grants(livekit_api.VideoGrants(room_join=True, room=room))
             .with_room_config(livekit_api.RoomConfiguration(
                 agents=[livekit_api.RoomAgentDispatch(agent_name=config.VOICE_AGENT)])))
    log.info("voice: token for room %s", room)
    return VoiceTokenResponse(server_url=config.LIVEKIT_URL, participant_token=token.to_jwt())


@app.get("/health", response_model=HealthResponse)
async def health_route():
    return HealthResponse(ok=True, model_service=await model_service_ok(),
                          webrtc_sessions=1 if webrtc.phones.current else 0, scene_memory=stream.live.running,
                          voice=bool(config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET))


# Stand-in until checkout lands: "pays" instantly and shows the success page.
@app.post("/checkout", response_model=CheckoutResponse)
async def checkout_route(req: CheckoutRequest):
    return CheckoutResponse(checkout_url=f"{config.PUBLIC_BASE_URL}/checkout/success")


@app.get("/checkout/success", response_class=HTMLResponse)
async def checkout_success_route():
    return ("<meta name=viewport content='width=device-width'>"
            "<h1 style='font-family:sans-serif;text-align:center;margin-top:40vh'>Order placed!</h1>")


# The live stream (stream.py): the projector page at /stream/viewer, its video and its events.
app.include_router(stream.router)
# The phone's live video over WebRTC (webrtc.py): GET /ice-servers, POST /offer. Tapping a hotspot is a
# Look (its DetectResponse isn't needed: the steps go to the phone and the projector as "look" events).
app.include_router(webrtc.router)
webrtc.select_handler = look
# Instant labels on the stream's objects, and background Looks so a pick shows results right away
# (prefetch.py). A purge (pause, camera switch, disconnect) forgets them with scene memory.
stream.live.on_objects = prefetch.observe
stream.live.object_fields = prefetch.fields
stream.live.on_purge = prefetch.reset
prefetch.start_look = background_look
