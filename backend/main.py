"""W6: routes, static uploads, capture store, timing.

    uvicorn main:app --reload --port 8000        (from backend/)

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

import config
import net
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
            headers={"Authorization": f"Bearer {config.MODEL_SERVICE_TOKEN}"},
        )
        return r.status_code == 200 and r.json().get("ok") is True
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

    timing: dict[str, int] = {}
    vision = stage("vision")
    with timed(timing, "vision"):
        item = await vision.detect(buf.getvalue()) if vision else Item.model_validate(fixture("item_lamp.json"))

    segmentation = None
    if item.category is None:
        item.quick_phrase = NO_ITEM_PHRASE
    else:
        segment = stage("segment")
        with timed(timing, "segment"):
            segmentation = (await segment.segment(img, item, capture_id) if segment
                            else Segmentation.model_validate(fixture("segmentation_lamp.json")))

    captures[capture_id] = Capture(
        capture_id=capture_id, image_path=image_path, image_url=image_url, item=item,
        segmentation=segmentation, image_sha256=hashlib.sha256(data).hexdigest(),
    )
    latency = ms_since(t0)
    log.info("detect %s category=%r %s total=%dms", capture_id, item.category, timing, latency)
    return DetectResponse(
        capture_id=capture_id, image_url=image_url, item=item, segmentation=segmentation,
        quick_phrase=item.quick_phrase, latency_ms=latency,
    )


async def run_search(capture_id: str) -> SearchResponse:
    t0 = time.perf_counter()
    cap = captures.get(capture_id)
    if cap is None:
        raise HTTPException(404, f"unknown capture_id {capture_id}")
    if config.USE_FIXTURES:
        return SearchResponse.model_validate(fixture("search_response.json") | {"capture_id": capture_id})
    if cap.item.category is None or cap.segmentation is None:
        return empty_search(capture_id)

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


# ---------- app ----------

config.UPLOADS.mkdir(exist_ok=True)
config.LOGS.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    install_fixtures()
    # Wake the Modal container now so the first real request isn't a cold start.
    wake = asyncio.create_task(model_service_ok(timeout=90.0))
    log.info("ready: fixtures=%s demo_mode=%s base=%s", config.USE_FIXTURES, config.DEMO_MODE,
             config.PUBLIC_BASE_URL)
    yield
    wake.cancel()
    await net.close()


app = FastAPI(title="Get the Look backend", lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=config.UPLOADS), name="uploads")


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
