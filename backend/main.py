"""W6: routes, static uploads, capture store, timing.

    uvicorn main:app --reload --port 8000 --timeout-graceful-shutdown 1        (from backend/)

(--timeout-graceful-shutdown 1: an open projector page streams forever, which would otherwise block
Ctrl+C and --reload.)

USE_FIXTURES=true  -> every route returns fixture JSON (for the iOS team).
USE_FIXTURES=false -> each stage runs once its module exists (vision.py, segment.py, ...);
                      until then that stage falls back to its fixture.
"""
import asyncio
import hashlib
import importlib
import io
import json
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager, contextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from livekit import api as livekit_api
from PIL import Image, ImageOps, UnidentifiedImageError

import cart
import config
import net
import stream
import webrtc
from models import (
    BBox, Capture, CheckoutRequest, CheckoutResponse, DetectResponse, HealthResponse,
    IdentifyResponse, Item, PriceInsight, Product, SearchResponse, Segmentation, VoiceTokenRequest,
    VoiceTokenResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gtl")

FIXTURE_ID = "c_fixture"
NO_ITEM_PHRASE = "I couldn't spot a product there. Try again closer."
MAX_HINT_TEXT = 100  # characters of hint_text kept: a SAM prompt and a line for Claude, not a paragraph

# fixture image -> name it is served under, matching the URLs in the fixture JSON
FIXTURE_IMAGES = {
    "lamp_room.jpg": f"{FIXTURE_ID}.jpg",
    "lamp_cutout.jpg": f"{FIXTURE_ID}_cutout.jpg",
    "lamp_mask.jpg": f"{FIXTURE_ID}_mask.jpg",
}

captures: dict[str, Capture] = {}
# /search work, started as soon as /detect finishes so it is underway (or done) when the app asks.
search_tasks: dict[str, asyncio.Task] = {}
# capture -> the live-stream object it looked at, so the projector can highlight that box
capture_tracks: dict[str, int] = {}

STAGES = ("vision", "segment", "search", "rerank", "pricing", "rank")


# ---------- helpers ----------

def fixture(name: str):
    text = (config.FIXTURES / name).read_text().replace("__BASE__", config.PUBLIC_BASE_URL)
    return json.loads(text)


def stage(name: str):
    """The stage's module once its file exists, else None (the caller uses the fixture).

    Checks for the file rather than catching ImportError, so a bug in a stage still crashes loudly.
    """
    return importlib.import_module(name) if (config.ROOT / f"{name}.py").exists() else None


def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


@contextmanager
def timed(timing: dict[str, int], key: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        timing[key] = ms_since(t0)


def load_image(data: bytes) -> Image.Image:
    """Decode the upload, applying EXIF rotation so bbox coordinates match what we save."""
    try:
        return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "field 'image' must be a JPEG")


def install_fixtures():
    for src, dst in FIXTURE_IMAGES.items():
        shutil.copyfile(config.FIXTURES / src, config.UPLOADS / dst)
    det = DetectResponse.model_validate(fixture("detect_response.json"))
    captures[FIXTURE_ID] = Capture(
        capture_id=FIXTURE_ID, image_path=f"uploads/{FIXTURE_ID}.jpg", image_url=det.image_url,
        item=det.item, segmentation=det.segmentation,
    )


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


def empty_search(capture_id: str, item: Item | None = None) -> SearchResponse:
    """/search for a capture where no item was detected."""
    return SearchResponse(
        capture_id=capture_id, best_match=None, dupes=[], used=[],
        price_insight=PriceInsight(
            typical_low=None, typical_high=None, median=None, sample_size=0,
            best_match_verdict="fair", verdict_text="",
        ),
        spoken_summary=NO_ITEM_PHRASE, timing_ms={"total": 0}, item=item,
    )


def announce(cap: Capture, step: str, **extra):
    """Tell the projector page (and anyone following the stream) how a Look is going. Steps:
    "detected" (label + crop), "described" (Claude's name), "results" (the cards), "failed", "nothing"."""
    stream.live.publish({"type": "look", "stage": step, "capture_id": cap.capture_id,
                         "track_id": capture_tracks.get(cap.capture_id), **extra})


def announce_detected(cap: Capture):
    if cap.segmentation is None:
        announce(cap, "nothing", quick_phrase=cap.item.quick_phrase)
    else:
        announce(cap, "detected", label=cap.item.category, quick_phrase=cap.item.quick_phrase,
                 box=cap.segmentation.bbox.model_dump(), cutout_path=cap.segmentation.cutout_path)


def parse_hint(hint_box: str | None, hint_label: str | None):
    """The object the app's user picked (a tapped hotspot): hint_box is JSON, normalized to the photo,
    either {"x", "y", "w", "h"} or [x1, y1, x2, y2] as in the stream's detections; hint_label is its
    label. Returns a segment.Found, or None."""
    if not hint_box:
        return None
    segment = stage("segment")
    try:
        raw = json.loads(hint_box)
        if isinstance(raw, list):
            x1, y1, x2, y2 = (float(v) for v in raw)
            raw = {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}
        box = segment.pad(BBox.model_validate(raw), 0.0)  # clamped to the photo
    except (ValueError, TypeError):
        raise HTTPException(400, 'hint_box must be JSON like {"x": 0.3, "y": 0.1, "w": 0.2, "h": 0.7} '
                                 'or [x1, y1, x2, y2]')
    if box.w <= 0 or box.h <= 0:
        raise HTTPException(400, "hint_box has no area inside the photo")
    return segment.Found(box, None, (hint_label or "").strip())


def parse_point(hint_point: str | None) -> tuple[float, float] | None:
    """The point the app's user tapped: JSON normalized to the photo, {"x", "y"} or [x, y]."""
    if not hint_point:
        return None
    try:
        raw = json.loads(hint_point)
        x, y = (float(v) for v in (raw if isinstance(raw, list) else (raw["x"], raw["y"])))
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, 'hint_point must be JSON like {"x": 0.45, "y": 0.6} or [x, y], normalized 0-1')
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise HTTPException(400, "hint_point must be inside the photo: x and y between 0 and 1")
    return x, y


def parse_text(hint_text: str | None) -> str | None:
    """What the user asked about, from their voice command (e.g. "green floor lamp"): SAM's prompt and a
    line for Claude. Kept to one short line."""
    return " ".join((hint_text or "").split())[:MAX_HINT_TEXT] or None


def parse_track(track_id: str) -> int:
    """A tracked object's id as the data channel sends it: "t4" -> 4."""
    try:
        return int(track_id.strip().removeprefix("t"))
    except ValueError:
        raise HTTPException(400, 'track_id must be a tracked object\'s id like "t4"')


async def detect_form(image: UploadFile | None, track_id: str | None, hint_box: str | None,
                      hint_label: str | None, hint_point: str | None, hint_text: str | None) -> DetectResponse:
    """/detect and /identify: a photo (image, with optional hints), or an object the live stream is
    tracking (track_id, no photo), looked at in the frame its box describes."""
    data = await image.read() if image else b""
    if bool(data) == bool(track_id):
        raise HTTPException(400, 'send exactly one of image (a JPEG) or track_id (e.g. "t4")')
    if track_id:
        resp = await look(parse_track(track_id))
        if resp is None:
            raise HTTPException(404, "track expired")
        return resp
    text = parse_text(hint_text)
    return await run_detect(data, hint=parse_hint(hint_box, hint_label or text), point=parse_point(hint_point),
                            text=text)


# ---------- pipeline ----------

async def run_detect(data: bytes, hint=None, track_id: int | None = None, point: tuple[float, float] | None = None,
                     text: str | None = None) -> DetectResponse:
    """hint: a segment.Found for the object the user picked (a tapped hotspot, or a tracked object);
    it replaces SAM's guess. track_id: the live-stream object this capture looks at, if any.
    point: where the user tapped (normalized), when there's no hint: SAM's object there.
    text: what the user asked about. Claude always gets it; SAM looks for it unless hint is given.
    Precedence: hint > point > text."""
    t0 = time.perf_counter()
    img = load_image(data)
    if config.USE_FIXTURES:
        return DetectResponse.model_validate(fixture("detect_response.json"))

    capture_id = "c_" + uuid.uuid4().hex[:8]
    if track_id is not None:
        capture_tracks[capture_id] = track_id
    image_path = f"uploads/{capture_id}.jpg"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    (config.ROOT / image_path).write_bytes(buf.getvalue())
    image_url = f"{config.PUBLIC_BASE_URL}/{image_path}"

    def done(item: Item, segmentation: Segmentation | None, path: str) -> DetectResponse:
        captures[capture_id] = Capture(
            capture_id=capture_id, image_path=image_path, image_url=image_url, item=item,
            segmentation=segmentation, image_sha256=hashlib.sha256(data).hexdigest(),
        )
        latency = ms_since(t0)
        log.info("detect %s %s category=%r %s total=%dms", capture_id, path, item.category, timing, latency)
        announce_detected(captures[capture_id])
        return DetectResponse(capture_id=capture_id, image_url=image_url, item=item,
                              segmentation=segmentation, quick_phrase=item.quick_phrase, latency_ms=latency)

    def answer_early(target, segmentation: Segmentation, claude: asyncio.Task, path: str) -> DetectResponse:
        """Answer with the object's label right away; Claude's description arrives in the background,
        before the search needs it."""
        label = target.label.strip()
        item = Item(category=label or "item", bbox=target.box, search_queries=[label] if label else [],
                    quick_phrase=f"{label[:1].upper()}{label[1:]}. Finding prices." if label
                    else "Got it. Finding prices.")
        resp = done(item, segmentation, path)
        search_tasks[capture_id] = asyncio.create_task(
            describe_then_search(captures[capture_id], claude, on_crop=path.startswith("picked"), hint=text))
        return resp

    def describe_crop(segmentation: Segmentation) -> asyncio.Task:
        return asyncio.create_task(vision.detect((config.ROOT / segmentation.cutout_path).read_bytes(), hint=text))

    timing: dict[str, int] = {}
    vision, segment = stage("vision"), stage("segment")
    if point and not hint and vision and segment:
        # The user tapped a point: SAM's object there (looking for what they asked about, if they said), else
        # a crop around the point. Either way, picked: Claude describes just the crop.
        with timed(timing, "sam"):
            found = await segment.sam_objects(img, text or segment.SAM_CATEGORIES or None,
                                              timeout=segment.SAM_FIRST_TIMEOUT)
        hint = segment.point_object(found or [], *point)
        if hint is None:
            with timed(timing, "crop"):
                segmentation = segment.point_crop(img, *point, capture_id, t0)
            target = segment.Found(segmentation.bbox, None, text or "")
            return answer_early(target, segmentation, describe_crop(segmentation), "picked-point-crop")
        segmentation = segment.sam_crop(img, hint, capture_id, t0)
        return answer_early(hint, segmentation, describe_crop(segmentation), "picked-point")
    if hint and vision and segment:
        # The user picked the object: no need to ask SAM, and Claude describes just its crop.
        segmentation = segment.sam_crop(img, hint, capture_id, t0)
        return answer_early(hint, segmentation, describe_crop(segmentation), "picked")

    # Claude starts now either way: SAM first needs its description, Claude first its whole answer.
    claude = asyncio.create_task(vision.detect(buf.getvalue(), hint=text)) if vision else None

    sam_failed = False
    if claude and segment and config.MODEL_SERVICE_URL:
        # SAM first: every object in view from SAM_CATEGORIES; the user is looking at the one at the center of
        # the view. With text, SAM looks for what the user asked about instead: the one at the center, else
        # the best match.
        with timed(timing, "sam"):
            found = await segment.sam_objects(img, text or segment.SAM_CATEGORIES or None,
                                              timeout=segment.SAM_FIRST_TIMEOUT)
        target = segment.text_object(found or []) if text else segment.center_object(found or [])
        if target:
            return answer_early(target, segment.sam_crop(img, target, capture_id, t0), claude,
                                "sam-text" if text else "sam-first")
        sam_failed = found is None  # down or too slow: don't ask it again below

    # Claude first: no segmentation server, or nothing buyable at the center of the view.
    with timed(timing, "vision"):
        item = await claude if claude else Item.model_validate(fixture("item_lamp.json"))
    segmentation = None
    if item.category is None:
        item.quick_phrase = NO_ITEM_PHRASE
    else:
        with timed(timing, "segment"):
            segmentation = (await segment.segment(img, item, capture_id, use_sam=not sam_failed) if segment
                            else Segmentation.model_validate(fixture("segmentation_lamp.json")))
    resp = done(item, segmentation, "claude-first")
    if segmentation is not None:
        # Start searching now; the app's /search call picks up the result (saves a round trip).
        search_tasks[capture_id] = asyncio.create_task(compute_search(captures[capture_id]))
    return resp


async def describe_then_search(cap: Capture, claude: asyncio.Task, on_crop: bool = False,
                               hint: str | None = None) -> SearchResponse:
    """The object is known (SAM first, or the user picked it): Claude describes it, then the store
    search runs on Claude's queries (a label like "lamp" alone finds lamps, not this one). on_crop:
    Claude was asked about the crop itself; otherwise about the whole photo, and then about the crop if
    it looked at a different object (with hint, what the user asked about). Claude failing leaves the label."""
    t0 = time.perf_counter()
    vision, segment = stage("vision"), stage("segment")
    sam_item = cap.item
    try:
        item = await claude
        if not on_crop and (item.category is None or item.bbox is None
                            or segment.iou(item.bbox, sam_item.bbox) <= segment.SAM_IOU):
            log.info("describe %s: Claude looked elsewhere; asking about SAM's crop", cap.capture_id)
            item = await vision.detect((config.ROOT / cap.segmentation.cutout_path).read_bytes(), hint=hint)
        if item.category:
            cap.item = item.model_copy(update={"bbox": sam_item.bbox, "quick_phrase": sam_item.quick_phrase})
            announce(cap, "described", item=cap.item.model_dump(mode="json"))
    except Exception as e:
        log.warning("describe %s: Claude failed (%r); searching with SAM's label", cap.capture_id, e)
    log.info("describe %s: %dms category=%r queries=%s", cap.capture_id, ms_since(t0),
             cap.item.category, cap.item.search_queries)
    return await compute_search(cap)


async def run_search(capture_id: str) -> SearchResponse:
    cap = captures.get(capture_id)
    if cap is None:
        raise HTTPException(404, f"unknown capture_id {capture_id}")
    if config.USE_FIXTURES:
        return SearchResponse.model_validate(fixture("search_response.json") | {"capture_id": capture_id})
    if cap.item.category is None or cap.segmentation is None:
        return empty_search(capture_id, cap.item)

    task = search_tasks.get(capture_id)
    if task is not None:
        t0 = time.perf_counter()
        try:
            # shield: an app that hangs up mid-request must not cancel the shared search
            resp = await asyncio.shield(task)
            log.info("search %s: started at /detect, waited %dms for it", capture_id, ms_since(t0))
            return resp
        except Exception:
            log.exception("search %s: early search failed; running it again", capture_id)
            search_tasks.pop(capture_id, None)
    return await compute_search(cap)


async def compute_search(cap: Capture) -> SearchResponse:
    """search -> rerank -> pricing + rank for one capture, with the finished item attached and the
    result announced to the projector."""
    try:
        resp = await search_pipeline(cap)
    except Exception:
        announce(cap, "failed", message="Couldn't get prices for that one.")
        raise
    resp.item = cap.item
    announce(cap, "results", search=resp.model_dump(mode="json"))
    return resp


async def search_pipeline(cap: Capture) -> SearchResponse:
    t0 = time.perf_counter()
    capture_id = cap.capture_id
    timing: dict[str, int] = {}
    cutout_url = cap.segmentation.cutout_url

    search = stage("search")
    with timed(timing, "search"):
        if search:
            products = await search.search_all(cutout_url, cap.item, capture_id=capture_id)
        else:
            products = [Product.model_validate(p) for p in fixture("products_raw_lamp.json")]

    rerank = stage("rerank")
    with timed(timing, "rerank"):
        if rerank:
            products = await rerank.rerank(cutout_url, products)
        elif not search:  # both stand-ins: use the matching scored fixture
            products = [Product.model_validate(p) for p in fixture("products_ranked_lamp.json")]
        # real search + no rerank yet: products pass through unscored

    pricing, rank = stage("pricing"), stage("rank")
    with timed(timing, "rank"):
        if pricing and rank:
            resp = await rank.choose(cap.item, products, pricing.compute_price_insight(products))
        else:
            resp = SearchResponse.model_validate(fixture("search_response.json"))

    resp.capture_id = capture_id
    resp.timing_ms = timing | {"total": ms_since(t0)}
    log.info("search %s products=%d timing=%s", capture_id, len(products), resp.timing_ms)
    return resp


async def look(track_id: int) -> DetectResponse | None:
    """The user picked a tracked object (the phone's "select" message, POST /look/{id}, or the voice
    assistant). The crop comes from that track's best frame in scene memory (sharpest, biggest, most
    centered), in view or not, with the box it had there, so SAM isn't asked again; the projector follows
    along through the "look" events. None if scene memory doesn't hold that track (any more)."""
    best = stream.live.scene.best(track_id)
    if best is None:
        log.info("look: scene memory has no object %d", track_id)
        return None
    frame, track = best
    buf = io.BytesIO()
    frame.convert("RGB").save(buf, format="JPEG", quality=90)
    hint = stage("segment").Found(track.frame_box, track.score, track.label)
    return await run_detect(buf.getvalue(), hint=hint, track_id=track_id)


async def warm_up():
    """Make the first real request as fast as later ones: import the stages now, and make one call per
    Claude stage (vision, rank) so each connection is open and the API has prepared its output schema
    (about a cent in total)."""
    for name in STAGES:
        try:
            stage(name)
        except Exception:
            log.exception("warm-up: stage %s failed to import", name)
    if not config.WARMUP or config.USE_FIXTURES:
        return
    vision, pricing, rank = stage("vision"), stage("pricing"), stage("rank")
    calls = []
    if vision:
        calls.append(("vision", vision.detect((config.FIXTURES / "lamp_room.jpg").read_bytes())))
    if pricing and rank:
        item = Item.model_validate(fixture("item_lamp.json"))
        products = [Product.model_validate(p) for p in fixture("products_ranked_lamp.json")]
        calls.append(("rank", rank.choose(item, products, pricing.compute_price_insight(products))))
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
    install_fixtures()
    # In the background, so the server accepts requests right away: wake the segmentation server
    # (no cold start on the first request), warm up our own stages, and get TURN credentials (the
    # phone's first /ice-servers is then fast).
    background = [asyncio.create_task(model_service_ok(timeout=90.0)), asyncio.create_task(warm_up()),
                  asyncio.create_task(webrtc.phones.ice.get())]
    log.info("ready: fixtures=%s demo_mode=%s warmup=%s ICE=%s base=%s", config.USE_FIXTURES, config.DEMO_MODE,
             config.WARMUP, webrtc.phones.ice.mode, config.PUBLIC_BASE_URL)
    yield
    for task in background + list(search_tasks.values()):
        task.cancel()
    await webrtc.close()      # the phone's connection
    await stream.live.stop()  # deletes the tracking session on the segmentation server
    await net.close()


app = FastAPI(title="Get the Look backend", lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=config.UPLOADS), name="uploads")
app.include_router(cart.router)


@app.post("/detect", response_model=DetectResponse)
async def detect_route(image: UploadFile | None = File(None), track_id: str | None = Form(None),
                       hint_box: str | None = Form(None), hint_label: str | None = Form(None),
                       hint_point: str | None = Form(None), hint_text: str | None = Form(None)):
    """Exactly one of image (a JPEG) or track_id ("t4": a tracked object, in its best frame; 404 "track
    expired" once it's gone). With an image, optional hints, in order of precedence:
    hint_box + hint_label, the object the user tapped (used instead of SAM's guess); hint_point, the point
    they tapped; hint_text, what they asked about (also passed to Claude)."""
    return await detect_form(image, track_id, hint_box, hint_label, hint_point, hint_text)


@app.post("/search/{capture_id}", response_model=SearchResponse)
async def search_route(capture_id: str):
    return await run_search(capture_id)


@app.post("/identify", response_model=IdentifyResponse)
async def identify_route(image: UploadFile | None = File(None), track_id: str | None = Form(None),
                         hint_box: str | None = Form(None), hint_label: str | None = Form(None),
                         hint_point: str | None = Form(None), hint_text: str | None = Form(None)):
    """/detect and /search in one call; the same inputs as /detect."""
    det = await detect_form(image, track_id, hint_box, hint_label, hint_point, hint_text)
    srch = await run_search(det.capture_id) if det.item.category else None
    return IdentifyResponse(detect=det, search=srch)


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
                          fixtures=config.USE_FIXTURES, demo_mode=config.DEMO_MODE,
                          webrtc_sessions=1 if webrtc.phones.current else 0, scene_memory=stream.live.running,
                          voice=bool(config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET))


# The live stream (stream.py): the projector page at /stream/viewer, its video and its events.
app.include_router(stream.router)
# The phone's live video over WebRTC (webrtc.py): GET /ice-servers, POST /offer. Tapping a hotspot is a
# Look (its DetectResponse isn't needed: the steps go to the phone and the projector as "look" events).
app.include_router(webrtc.router)
webrtc.select_handler = look


_checkout = None if config.USE_FIXTURES else stage("checkout")
if _checkout:
    app.include_router(_checkout.router)
else:
    # Stand-in until A2's checkout.py lands: "pays" instantly and shows the success page.
    @app.post("/checkout", response_model=CheckoutResponse)
    async def checkout_stub(req: CheckoutRequest):
        return CheckoutResponse(checkout_url=f"{config.PUBLIC_BASE_URL}/checkout/success")

    @app.get("/checkout/success", response_class=HTMLResponse)
    async def checkout_success_stub():
        return ("<meta name=viewport content='width=device-width'>"
                "<h1 style='font-family:sans-serif;text-align:center;margin-top:40vh'>Order placed!</h1>")
