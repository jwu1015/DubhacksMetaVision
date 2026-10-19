"""SAM 3.x streaming segmentation + tracking API server.

Tracking is stateful: create a session, then post frames in order. Object ids stay stable across
the frames of a session. A session's labels (its SAM prompts) are given when it is created and stay
fixed for the whole session (the backend picks them, with Claude, from the first frame: backend/labels.py).

  POST   /session              {"prompt": "cup, laptop"} or {"prompt": ["cup", "laptop"]} -> {"session_id", "labels"}
  POST   /session/{id}/frame   raw JPEG body -> {"frame_idx", "width", "height", "labels", "objects": [{"id", "score", "box", "prompt"}]}
                                409 if a newer frame for the session arrived while this one waited (it was skipped)
  DELETE /session/{id}
  GET    /health

Boxes are [x1, y1, x2, y2] in pixels of the submitted frame.
Config via env vars: SAM_MODEL, SAM_THRESHOLD, MERGE_IOU, NEST_FRACTION, MAX_SESSIONS,
SESSION_TTL, KEEP_FRAMES, LOST_FRAMES, MAX_OBJECTS, SAM_DTYPE, BATCH_PROMPTS, HOST, PORT, ROOT_PATH.
Run: python server.py
"""
import io
import logging
import os
import threading
import time
import types
import uuid

import torch
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from transformers import Sam3VideoConfig, Sam3VideoModel, Sam3VideoProcessor
from transformers.models.sam3_video.modeling_sam3_video import nms_masks

MODEL_ID = os.environ.get("SAM_MODEL", "facebook/sam3.1")
log = logging.getLogger("uvicorn.error")  # uvicorn's logger, so messages show in the server output

THRESHOLD = float(os.environ.get("SAM_THRESHOLD", "0.5"))
# Boxes overlapping at least this much (IoU) are treated as one object matched by several
# prompts (e.g. "sofa" and "furniture"); only the highest-scoring one is kept.
MERGE_IOU = float(os.environ.get("MERGE_IOU", "0.7"))
# A box lying at least this much (by its own area) inside a bigger box is left out of the response,
# e.g. a "keyboard" box inside the "laptop" box. 1 or more = never.
NEST_FRACTION = float(os.environ.get("NEST_FRACTION", "0.9"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "4"))
SESSION_TTL = float(os.environ.get("SESSION_TTL", "120"))  # seconds idle before a session is evicted
# Per-frame state older than this many frames is dropped. The tracker's mask memory reads the last
# 6 frames and its object pointers up to 15 back; pointers older than KEEP_FRAMES are simply not
# used. The 4 most recent conditioning frames are always kept.
KEEP_FRAMES = max(7, int(os.environ.get("KEEP_FRAMES", "10")))
# Stop tracking an object once the detector hasn't confirmed it for this many frames (0 = never).
# In streaming mode the model never drops objects on its own, so without this every object ever seen
# stays tracked: per-frame time keeps climbing and drifted tracks can latch onto other things.
LOST_FRAMES = int(os.environ.get("LOST_FRAMES", "30"))
# Most objects tracked at once per session. When full, new detections are ignored until a tracked
# object is lost (LOST_FRAMES) or merged away as a duplicate; then the highest-scoring new one gets in.
MAX_OBJECTS = int(os.environ.get("MAX_OBJECTS", "30"))
if MAX_OBJECTS < 1:
    raise SystemExit("MAX_OBJECTS must be at least 1")

if torch.cuda.is_available():
    device = "cuda"
elif torch.backends.mps.is_available():
    device = "mps"
else:
    device = "cpu"
# bf16 halves frame time on the GPU with the same detections (measured 17 Oct: 1.7 s -> 0.8 s at 41 phrases).
DTYPE = {"bf16": torch.bfloat16, "fp32": torch.float32}[os.environ.get("SAM_DTYPE", "bf16" if device == "cuda" else "fp32")]

config = Sam3VideoConfig.from_pretrained(MODEL_ID)
config.score_threshold_detection = THRESHOLD
# The model logs "hitting max_num_objects" on every frame while the cap is full; that's expected here.
logging.getLogger("transformers.models.sam3_video.modeling_sam3_video").addFilter(
    lambda r: "max_num_objects" not in r.getMessage()
)
processor = Sam3VideoProcessor.from_pretrained(MODEL_ID)
model = Sam3VideoModel.from_pretrained(MODEL_ID, config=config, dtype=DTYPE).to(device).eval()


def parse_labels(prompt) -> list[str]:
    """'cup, laptop' or ['cup', 'laptop'] -> ['cup', 'laptop']; None if it isn't usable."""
    if isinstance(prompt, str):
        prompt = prompt.split(",")
    if not isinstance(prompt, list) or not all(isinstance(p, str) for p in prompt):
        return None
    labels = list(dict.fromkeys(p.strip() for p in prompt if p.strip()))
    return labels or None


def _repeat_batch(x, n):
    if isinstance(x, (tuple, list)):
        return type(x)(_repeat_batch(t, n) for t in x)
    return x.expand(n, *x.shape[1:]) if torch.is_tensor(x) and x.shape[:1] == (1,) else x


def batched_run_detection(self, inference_session, vision_embeds):
    """Sam3VideoModel.run_detection, but every prompt goes through the detector in one batch instead of
    one call per prompt. The image features are shared, so this costs about one call, not one per phrase."""
    prompt_ids = list(inference_session.prompts.keys())
    if len(prompt_ids) <= 1:
        return Sam3VideoModel.run_detection(self, inference_session, vision_embeds)
    cached = getattr(inference_session, "batched_text", None)
    if cached is None or cached[0] != prompt_ids:
        input_ids = torch.cat([inference_session.prompt_input_ids[p] for p in prompt_ids])
        mask = torch.cat([inference_session.prompt_attention_masks[p] for p in prompt_ids])
        text = self.detector_model.get_text_features(input_ids=input_ids, attention_mask=mask, return_dict=True)
        cached = inference_session.batched_text = (prompt_ids, text, mask)
    _, text, mask = cached
    n = len(prompt_ids)
    vision = type(vision_embeds)(**{k: _repeat_batch(v, n) for k, v in vision_embeds.items()})
    out = self.detector_model(vision_embeds=vision, text_embeds=text, attention_mask=mask)

    # From here on, the same per-prompt steps as the original, on row i of the batch.
    probs = out.pred_logits.sigmoid() * out.presence_logits.sigmoid()
    detections = {}
    for i, prompt_id in enumerate(prompt_ids):
        pred_probs = probs[i : i + 1]
        if self.det_nms_thresh > 0.0:
            keep = nms_masks(
                pred_probs=pred_probs[0], pred_masks=out.pred_masks[i],
                prob_threshold=self.score_threshold_detection, iou_threshold=self.det_nms_thresh,
            )
            pred_probs = pred_probs.clone()
            pred_probs[0][~keep] = 0.0
        pos = torch.where(pred_probs > self.score_threshold_detection)
        detections[prompt_id] = {
            "bbox": out.pred_boxes[i : i + 1][pos[0], pos[1]],
            "mask": out.pred_masks[i : i + 1][pos[0], pos[1]],
            "scores": pred_probs[pos[0], pos[1]],
        }
    return detections


if os.environ.get("BATCH_PROMPTS", "1") == "1":
    model.run_detection = types.MethodType(batched_run_detection, model)

ROOT_PATH = os.environ.get("ROOT_PATH", "").rstrip("/")


class StripPrefix:
    """Serve the app under ROOT_PATH (e.g. "/inference" behind a tunnel); unprefixed paths still work."""

    def __init__(self, app, prefix):
        self.app, self.prefix = app, prefix

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and (path == self.prefix or path.startswith(self.prefix + "/")):
            scope = {**scope, "path": path[len(self.prefix):] or "/", "root_path": scope.get("root_path", "") + self.prefix}
        await self.app(scope, receive, send)


app = FastAPI(title="SAM segmentation server")
model_lock = threading.Lock()  # one forward pass at a time
sessions = {}  # id -> Session
sessions_lock = threading.Lock()


class Session:
    def __init__(self, labels, max_objects=MAX_OBJECTS):
        self.inference = processor.init_video_session(
            inference_device=device, processing_device="cpu", video_storage_device="cpu", dtype=DTYPE
        )
        self.max_objects = max_objects
        self.arrived = 0  # frames received so far; a frame waiting behind a newer one is skipped
        self.labels = labels  # SAM prompts, fixed for the session
        processor.add_text_prompt(inference_session=self.inference, text=labels)
        self.lock = threading.Lock()  # frames of one session must be processed in order
        self.last_seen = {}  # obj id -> last frame where the detector confirmed it
        self.shown = set()  # ids returned on the previous frame, preferred when merging duplicates
        self.last_used = time.monotonic()


def decode(data: bytes) -> Image.Image:
    if not data:
        raise HTTPException(400, "empty body; send raw JPEG bytes")
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "could not decode image")


def iou(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def drop_nested(objects):
    """Leave out boxes that lie mostly (NEST_FRACTION of their area) inside a bigger box."""
    def nested(obj):
        a = area(obj["box"])
        for other in objects:
            if other is obj or area(other["box"]) <= a:
                continue
            w = min(obj["box"][2], other["box"][2]) - max(obj["box"][0], other["box"][0])
            h = min(obj["box"][3], other["box"][3]) - max(obj["box"][1], other["box"][1])
            if a > 0 and w > 0 and h > 0 and w * h / a >= NEST_FRACTION:
                return True
        return False
    return [o for o in objects if not nested(o)]


def merge_duplicates(objects, prefer=()):
    """Keep one box per physical object. Ids returned last frame win, so the id doesn't flip between
    overlapping tracks (e.g. "sofa" vs "furniture") as their scores fluctuate."""
    kept = []
    for obj in sorted(objects, key=lambda o: (o["id"] in prefer, o["score"]), reverse=True):
        if all(iou(obj["box"], k["box"]) < MERGE_IOU for k in kept):
            kept.append(obj)
    return kept


def prune_old_frames(inf, frame_idx):
    """Free per-frame tracking state the model no longer reads, so long streams don't grow memory."""
    cutoff = frame_idx - KEEP_FRAMES
    if cutoff <= 0:
        return
    # Keep the keys: new frame numbers come from len(processed_frames).
    for k, v in inf.processed_frames.items():
        if k < cutoff and v is not None:
            inf.processed_frames[k] = None
    max_cond = model.tracker_model.config.max_cond_frame_num
    for out in inf.output_dict_per_obj.values():
        for k in [k for k in out["non_cond_frame_outputs"] if k < cutoff]:
            del out["non_cond_frame_outputs"][k]
        cond = out["cond_frame_outputs"]
        keep = set(sorted(cond)[-max_cond:]) if max_cond > 0 else set(cond)
        for k in [k for k in cond if k < cutoff and k not in keep]:
            del cond[k]
    for frames in inf.frames_tracked_per_obj.values():
        for k in [k for k in frames if k < cutoff]:
            del frames[k]
    for per_frame in (inf.obj_id_to_tracker_score_frame_wise, inf.suppressed_obj_ids):  # both keyed by frame
        for k in [k for k in per_frame if k < cutoff]:
            del per_frame[k]
    # Frame-index lists used only by the offline hotstart heuristics (off when streaming). They are
    # deep-copied every frame, so letting them grow makes each frame slower.
    for lists in (inf.unmatched_frame_inds, inf.overlap_pair_to_frame_inds):
        for key, frames in lists.items():
            lists[key] = [k for k in frames if k >= cutoff]


def stop_tracking(session, obj_id):
    """Remove one object from the tracker, freeing its slot."""
    inf = session.inference
    if obj_id not in inf.obj_ids:
        return
    inf.remove_object(obj_id)  # same call the model uses for its own removals
    session.last_seen.pop(obj_id, None)
    for d in (inf.obj_id_to_score, inf.obj_id_to_last_occluded, inf.trk_keep_alive,
              inf.unmatched_frame_inds, inf.obj_first_frame_idx):
        d.pop(obj_id, None)
    for pair in [p for p in inf.overlap_pair_to_frame_inds if obj_id in p]:
        del inf.overlap_pair_to_frame_inds[pair]


def drop_lost_objects(session, frame_idx, visible_ids):
    """Update when each object was last confirmed by the detector and stop tracking stale ones."""
    inf = session.inference
    for obj_id in visible_ids:
        # Visible but unmatched means only the tracker sees it, which is how drift starts.
        if frame_idx not in inf.unmatched_frame_inds.get(obj_id, ()):
            session.last_seen[obj_id] = frame_idx
    if LOST_FRAMES <= 0:
        return
    for obj_id in list(inf.obj_ids):
        if frame_idx - session.last_seen.setdefault(obj_id, frame_idx) > LOST_FRAMES:
            stop_tracking(session, obj_id)


def process_frame(session: Session, image: Image.Image, seq=None):
    with session.lock:
        session.last_used = time.monotonic()
        if seq is not None and seq != session.arrived:
            # Frames arrive faster than we process them: only the newest waiting one is worth tracking.
            raise HTTPException(409, "skipped: a newer frame arrived; send one frame at a time")
        with model_lock, torch.inference_mode():
            model.max_num_objects = session.max_objects  # per session; safe under model_lock
            inputs = processor(images=image, device=device, return_tensors="pt").to(model.device)
            frame = inputs.pixel_values[0].to(DTYPE)
            outputs = model(inference_session=session.inference, frame=frame, reverse=False)
            result = processor.postprocess_outputs(
                session.inference, outputs, original_sizes=inputs.original_sizes
            )
            # The model's own counter, so a failed frame can't knock our numbering out of sync.
            frame_idx = outputs.frame_idx
            ids = result["object_ids"].tolist()
            id_to_prompt = {i: p for p, ids_ in result["prompt_to_obj_ids"].items() for i in ids_}
            scores = result["scores"].float().tolist()
            boxes = result["boxes"].float().tolist()
            objects = merge_duplicates([
                {"id": i, "score": round(s, 4), "box": [round(v, 1) for v in b], "prompt": id_to_prompt.get(i)}
                for i, s, b in zip(ids, scores, boxes)
            ], prefer=session.shown)
            session.shown = {o["id"] for o in objects}
            # A track merged away is a second copy of a kept object; drop it so it doesn't hold a slot.
            for obj_id in set(ids) - session.shown:
                stop_tracking(session, obj_id)
            drop_lost_objects(session, frame_idx, session.shown)
            # Nested boxes are only hidden, not untracked: the detector keeps finding them, so a
            # removed track would come back as a new object on the next frame.
            objects = drop_nested(objects)
            prune_old_frames(session.inference, frame_idx)
        session.last_used = time.monotonic()
    return {"frame_idx": frame_idx, "width": image.width, "height": image.height,
            "labels": session.labels, "objects": objects}


def evict_expired():
    now = time.monotonic()
    with sessions_lock:
        for sid in [k for k, s in sessions.items() if now - s.last_used > SESSION_TTL]:
            del sessions[sid]


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_ID, "device": device, "sessions": len(sessions)}


def add_session(labels):
    evict_expired()
    with sessions_lock:
        if len(sessions) >= MAX_SESSIONS:
            raise HTTPException(503, f"too many sessions (max {MAX_SESSIONS}); delete one or wait for expiry")
        sid = uuid.uuid4().hex
        sessions[sid] = Session(labels)
    return sid


@app.post("/session")
def create_session(body: dict = Body(...)):
    labels = parse_labels(body.get("prompt"))
    if labels is None:
        raise HTTPException(400, 'prompt is required: "cup, laptop" or ["cup", "laptop"]')
    return {"session_id": add_session(labels), "labels": labels}


@app.post("/session/{sid}/frame")
async def session_frame(sid: str, request: Request):
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(404, "unknown or expired session")
    image = decode(await request.body())
    session.arrived += 1  # the event loop is single-threaded, so this is safe without a lock
    return await run_in_threadpool(process_frame, session, image, session.arrived)


@app.delete("/session/{sid}")
def delete_session(sid: str):
    with sessions_lock:
        if sessions.pop(sid, None) is None:
            raise HTTPException(404, "unknown or expired session")
    return {"deleted": sid}


if __name__ == "__main__":
    uvicorn.run(
        StripPrefix(app, ROOT_PATH) if ROOT_PATH else app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
