"""SAM 3.x streaming segmentation + tracking API server.

Tracking is stateful: create a session with a prompt, then post frames in order.
Object ids stay stable across the frames of a session.

  POST   /session              {"prompt": "person"} or {"prompt": ["person", "cup"]}  -> {"session_id": ...}
  POST   /session/{id}/frame   raw JPEG body -> {"frame_idx", "width", "height", "objects": [{"id", "score", "box", "prompt"}]}
  DELETE /session/{id}
  POST   /segment              raw JPEG body (+ ?prompt=) -> one-shot, no tracking (ids are per-frame)
  GET    /health

Boxes are [x1, y1, x2, y2] in pixels of the submitted frame.
Config via env vars: SAM_MODEL, SAM_PROMPT (comma-separated phrases), SAM_THRESHOLD, MERGE_IOU, MAX_SESSIONS, SESSION_TTL, HOST, PORT, ROOT_PATH.
Run: python server.py
"""
import io
import os
import threading
import time
import uuid

import torch
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from transformers import Sam3VideoConfig, Sam3VideoModel, Sam3VideoProcessor

MODEL_ID = os.environ.get("SAM_MODEL", "facebook/sam3.1")


def parse_prompt(text):
    """'chair, lamp' -> ['chair', 'lamp']; a single phrase stays a string."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    return parts[0] if len(parts) == 1 else parts


# Comma-separated noun phrases; each one is detected separately.
DEFAULT_PROMPT = parse_prompt(os.environ.get("SAM_PROMPT", "person"))
THRESHOLD = float(os.environ.get("SAM_THRESHOLD", "0.5"))
# Boxes overlapping at least this much (IoU) are treated as one object matched by several
# prompts (e.g. "sofa" and "furniture"); only the highest-scoring one is kept.
MERGE_IOU = float(os.environ.get("MERGE_IOU", "0.7"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "4"))
SESSION_TTL = float(os.environ.get("SESSION_TTL", "120"))  # seconds idle before a session is evicted

if torch.cuda.is_available():
    device = "cuda"
elif torch.backends.mps.is_available():
    device = "mps"
else:
    device = "cpu"

config = Sam3VideoConfig.from_pretrained(MODEL_ID)
config.score_threshold_detection = THRESHOLD
processor = Sam3VideoProcessor.from_pretrained(MODEL_ID)
model = Sam3VideoModel.from_pretrained(MODEL_ID, config=config).to(device).eval()

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
    def __init__(self, prompts):
        self.inference = processor.init_video_session(
            inference_device=device, processing_device="cpu", video_storage_device="cpu"
        )
        processor.add_text_prompt(inference_session=self.inference, text=prompts)
        self.lock = threading.Lock()  # frames of one session must be processed in order
        self.frame_idx = 0
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


def merge_duplicates(objects):
    kept = []
    for obj in sorted(objects, key=lambda o: o["score"], reverse=True):
        if all(iou(obj["box"], k["box"]) < MERGE_IOU for k in kept):
            kept.append(obj)
    return kept


def process_frame(session: Session, image: Image.Image):
    with session.lock:
        session.last_used = time.monotonic()
        with model_lock, torch.inference_mode():
            inputs = processor(images=image, device=device, return_tensors="pt").to(model.device)
            outputs = model(inference_session=session.inference, frame=inputs.pixel_values[0], reverse=False)
            result = processor.postprocess_outputs(
                session.inference, outputs, original_sizes=inputs.original_sizes
            )
        frame_idx = session.frame_idx
        session.frame_idx += 1
        session.last_used = time.monotonic()

    id_to_prompt = {i: p for p, ids in result["prompt_to_obj_ids"].items() for i in ids}
    ids = result["object_ids"].tolist()
    scores = result["scores"].float().tolist()
    boxes = result["boxes"].float().tolist()
    return {
        "frame_idx": frame_idx,
        "width": image.width,
        "height": image.height,
        "objects": merge_duplicates([
            {"id": i, "score": round(s, 4), "box": [round(v, 1) for v in b], "prompt": id_to_prompt.get(i)}
            for i, s, b in zip(ids, scores, boxes)
        ]),
    }


def evict_expired():
    now = time.monotonic()
    with sessions_lock:
        for sid in [k for k, s in sessions.items() if now - s.last_used > SESSION_TTL]:
            del sessions[sid]


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_ID, "device": device, "sessions": len(sessions)}


@app.post("/session")
def create_session(body: dict = Body(default={})):
    prompts = body.get("prompt", DEFAULT_PROMPT)
    if not (isinstance(prompts, str) or (isinstance(prompts, list) and prompts and all(isinstance(p, str) for p in prompts))):
        raise HTTPException(400, "prompt must be a string or a non-empty list of strings")
    evict_expired()
    with sessions_lock:
        if len(sessions) >= MAX_SESSIONS:
            raise HTTPException(503, f"too many sessions (max {MAX_SESSIONS}); delete one or wait for expiry")
        sid = uuid.uuid4().hex
        sessions[sid] = Session(prompts)
    return {"session_id": sid, "prompt": prompts}


@app.post("/session/{sid}/frame")
async def session_frame(sid: str, request: Request):
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(404, "unknown or expired session")
    image = decode(await request.body())
    return await run_in_threadpool(process_frame, session, image)


@app.delete("/session/{sid}")
def delete_session(sid: str):
    with sessions_lock:
        if sessions.pop(sid, None) is None:
            raise HTTPException(404, "unknown or expired session")
    return {"deleted": sid}


@app.post("/segment")
async def segment(request: Request, prompt: str | None = None):
    prompt = parse_prompt(prompt) if prompt else DEFAULT_PROMPT
    image = decode(await request.body())
    result = await run_in_threadpool(lambda: process_frame(Session(prompt), image))
    result.pop("frame_idx")
    return result


if __name__ == "__main__":
    uvicorn.run(
        StripPrefix(app, ROOT_PATH) if ROOT_PATH else app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
