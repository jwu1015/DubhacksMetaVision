"""Looks: what a tracked object is and where to buy it. The crop comes from the object's best frame in
scene memory; Claude describes it, then the stores are searched on Claude's queries. Each step goes out
as a "look" event on the stream.
"""
import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable
from contextlib import contextmanager

from storefront import config
from storefront.live import prefetch, stream
from storefront.models import Capture, DetectResponse, Item, SearchResponse
from storefront.pipeline import crop, pricing, rank, rerank, search, vision

log = logging.getLogger("storefront.looks")

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
