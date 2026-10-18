"""The Storefront backend: builds the FastAPI app and wires its parts together.

    uvicorn storefront.app:app --reload --port 8000 --timeout-graceful-shutdown 1        (from backend/)

(--timeout-graceful-shutdown 1: an open projector page streams forever, which would otherwise block
Ctrl+C and --reload.)
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from storefront import config, net
from storefront.api import cart, checkout, health, look, session, viewer
from storefront.live import stream
from storefront.live.trigger import Dwell
from storefront.models import Item, Product
from storefront.pipeline import pricing, rank, vision
from storefront.pipeline.looks import Looks
from storefront.pipeline.runner import Runner
from storefront.rtc import legacy_webrtc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("storefront")


def fixture(name: str):
    return json.loads((config.FIXTURES / name).read_text().replace("__BASE__", config.PUBLIC_BASE_URL))


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
            log.info("warm-up: %s call %dms", name, round((time.perf_counter() - t0) * 1000))
        except Exception as e:
            log.warning("warm-up: %s call failed: %r", name, e)


def wire() -> Runner:
    """Connect the parts: every tracking result feeds the pipeline (quick labels and Looks), a purge (pause,
    camera switch, disconnect) resets it, and a tap on a hotspot picks an object."""
    runner = Runner(stream.live, Looks(stream.live.publish), Dwell(config.QUICK_LABEL_DWELL_MS, config.LOOK_DWELL_MS))
    stream.live.on_result = runner.observe
    stream.live.on_purge = runner.reset
    legacy_webrtc.select_handler = runner.select
    return runner


@asynccontextmanager
async def lifespan(app: FastAPI):
    # In the background, so the server accepts requests right away: wake the segmentation server
    # (no cold start on the first request), warm up our own stages, and get TURN credentials (the
    # phone's first /ice-servers is then fast).
    background = [asyncio.create_task(health.model_service_ok(timeout=90.0)), asyncio.create_task(warm_up()),
                  asyncio.create_task(legacy_webrtc.phones.ice.get())]
    log.info("ready: warmup=%s ICE=%s base=%s", config.WARMUP, legacy_webrtc.phones.ice.mode,
             config.PUBLIC_BASE_URL)
    yield
    for task in background:
        task.cancel()
    await legacy_webrtc.close()  # the phone's connection
    await stream.live.stop()     # deletes the tracking session on the segmentation server
    await net.close()


config.UPLOADS.mkdir(exist_ok=True)
config.LOGS.mkdir(exist_ok=True)

app = FastAPI(title="Storefront backend", lifespan=lifespan)
app.mount("/uploads", StaticFiles(directory=config.UPLOADS), name="uploads")
for module in (cart, checkout, health, look, session, viewer, legacy_webrtc):
    app.include_router(module.router)
app.state.runner = wire()
