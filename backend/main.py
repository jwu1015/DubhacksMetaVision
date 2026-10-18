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

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError

import cart
import config
import net
import stream
from models import (
    Capture, CheckoutRequest, CheckoutResponse, DetectResponse, HealthResponse,
    IdentifyResponse, Item, PriceInsight, Product, SearchResponse, Segmentation,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gtl")

FIXTURE_ID = "c_fixture"
NO_ITEM_PHRASE = "I couldn't spot a product there. Try again closer."

# fixture image -> name it is served under, matching the URLs in the fixture JSON
FIXTURE_IMAGES = {
    "lamp_room.jpg": f"{FIXTURE_ID}.jpg",
    "lamp_cutout.jpg": f"{FIXTURE_ID}_cutout.jpg",
    "lamp_mask.jpg": f"{FIXTURE_ID}_mask.jpg",
}

captures: dict[str, Capture] = {}
# /search work, started as soon as /detect finishes so it is underway (or done) when the app asks.
search_tasks: dict[str, asyncio.Task] = {}

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


def empty_search(capture_id: str) -> SearchResponse:
    """/search for a capture where no item was detected."""
    return SearchResponse(
        capture_id=capture_id, best_match=None, dupes=[], used=[],
        price_insight=PriceInsight(
            typical_low=None, typical_high=None, median=None, sample_size=0,
            best_match_verdict="fair", verdict_text="",
        ),
        spoken_summary=NO_ITEM_PHRASE, timing_ms={"total": 0},
    )


# ---------- pipeline ----------

async def run_detect(data: bytes) -> DetectResponse:
    t0 = time.perf_counter()
    img = load_image(data)
    if config.USE_FIXTURES:
        return DetectResponse.model_validate(fixture("detect_response.json"))

    capture_id = "c_" + uuid.uuid4().hex[:8]
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
        return DetectResponse(capture_id=capture_id, image_url=image_url, item=item,
                              segmentation=segmentation, quick_phrase=item.quick_phrase, latency_ms=latency)

    timing: dict[str, int] = {}
    vision, segment = stage("vision"), stage("segment")
    # Claude starts now either way: SAM first needs its description, Claude first its whole answer.
    claude = asyncio.create_task(vision.detect(buf.getvalue())) if vision else None

    sam_failed = False
    if claude and segment and config.MODEL_SERVICE_URL:
        # SAM first: every object in view from SAM_CATEGORIES; the user is looking at the one at the center of
        # the view. Answer with SAM's label right away; Claude's description arrives in the
        # background, before the search needs it.
        with timed(timing, "sam"):
            found = await segment.sam_objects(img, segment.SAM_CATEGORIES or None,
                                              timeout=segment.SAM_FIRST_TIMEOUT)
        target = segment.center_object(found or [])
        if target:
            label = target.label.strip()
            item = Item(category=label, bbox=target.box, search_queries=[label],
                        quick_phrase=f"{label[:1].upper()}{label[1:]}. Finding prices.")
            resp = done(item, segment.sam_crop(img, target, capture_id, t0), "sam-first")
            search_tasks[capture_id] = asyncio.create_task(describe_then_search(captures[capture_id], claude))
            return resp
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


def start_search(cap: Capture) -> asyncio.Task | None:
    """Start the store search now, if the search stage exists. It needs only the crop and a label:
    Lens on the crop, then Shopping and eBay from Lens's titles."""
    search = stage("search")
    if search is None:
        return None
    return asyncio.create_task(search.search_all(cap.segmentation.cutout_url, cap.item, capture_id=cap.capture_id))


async def describe_then_search(cap: Capture, claude: asyncio.Task) -> SearchResponse:
    """SAM first: the store search starts right away on SAM's crop and label, while Claude describes
    the item (asking about SAM's crop if Claude looked at a different object) for ranking. Claude
    failing leaves SAM's label."""
    t0 = time.perf_counter()
    products = start_search(cap)
    vision, segment = stage("vision"), stage("segment")
    sam_item = cap.item
    try:
        item = await claude
        if item.category is None or item.bbox is None or segment.iou(item.bbox, sam_item.bbox) <= segment.SAM_IOU:
            log.info("describe %s: Claude looked elsewhere; asking about SAM's crop", cap.capture_id)
            item = await vision.detect((config.ROOT / cap.segmentation.cutout_path).read_bytes())
        if item.category:
            cap.item = item.model_copy(update={"bbox": sam_item.bbox, "quick_phrase": sam_item.quick_phrase})
    except Exception as e:
        log.warning("describe %s: Claude failed (%r); searching with SAM's label", cap.capture_id, e)
    log.info("describe %s: %dms category=%r queries=%s", cap.capture_id, ms_since(t0),
             cap.item.category, cap.item.search_queries)
    return await compute_search(cap, products)


async def run_search(capture_id: str) -> SearchResponse:
    cap = captures.get(capture_id)
    if cap is None:
        raise HTTPException(404, f"unknown capture_id {capture_id}")
    if config.USE_FIXTURES:
        return SearchResponse.model_validate(fixture("search_response.json") | {"capture_id": capture_id})
    if cap.item.category is None or cap.segmentation is None:
        return empty_search(capture_id)

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


async def compute_search(cap: Capture, products_task: asyncio.Task | None = None) -> SearchResponse:
    """search -> rerank -> pricing + rank for one capture. products_task: a search already started
    (timing_ms["search"] is then only the time left to wait for it)."""
    t0 = time.perf_counter()
    capture_id = cap.capture_id
    timing: dict[str, int] = {}
    cutout_url = cap.segmentation.cutout_url

    search = stage("search")
    with timed(timing, "search"):
        if products_task is not None:
            products = await products_task
        elif search:
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
    # (no cold start on the first request) and warm up our own stages.
    background = [asyncio.create_task(model_service_ok(timeout=90.0)), asyncio.create_task(warm_up())]
    log.info("ready: fixtures=%s demo_mode=%s warmup=%s base=%s", config.USE_FIXTURES, config.DEMO_MODE,
             config.WARMUP, config.PUBLIC_BASE_URL)
    yield
    for task in background + list(search_tasks.values()):
        task.cancel()
    await stream.live.stop()  # deletes the tracking session on the segmentation server
    await net.close()


app = FastAPI(title="Get the Look backend", lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=config.UPLOADS), name="uploads")
app.include_router(cart.router)


@app.post("/detect", response_model=DetectResponse)
async def detect_route(image: UploadFile = File(...)):
    return await run_detect(await image.read())


@app.post("/search/{capture_id}", response_model=SearchResponse)
async def search_route(capture_id: str):
    return await run_search(capture_id)


@app.post("/identify", response_model=IdentifyResponse)
async def identify_route(image: UploadFile = File(...)):
    det = await run_detect(await image.read())
    srch = await run_search(det.capture_id) if det.item.category else None
    return IdentifyResponse(detect=det, search=srch)


@app.get("/health", response_model=HealthResponse)
async def health_route():
    return HealthResponse(ok=True, model_service=await model_service_ok(),
                          fixtures=config.USE_FIXTURES, demo_mode=config.DEMO_MODE)


# The live stream (stream.py): the projector page at /stream/viewer, its video and its events.
app.include_router(stream.router)


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
