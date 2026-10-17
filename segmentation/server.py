"""SAM 3.x segmentation API server.

POST /segment  (body = raw JPEG bytes, Content-Type: image/jpeg)
  optional query params: prompt, threshold
  -> {"width": W, "height": H, "prompt": "...", "objects": [{"id": 0, "score": 0.9, "box": [x1, y1, x2, y2]}, ...]}

Config via env vars: SAM_MODEL, SAM_PROMPT, SAM_THRESHOLD, HOST, PORT.
Run: python server.py
"""
import io
import os
import threading

import torch
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from PIL import Image, UnidentifiedImageError
from transformers import Sam3Model, Sam3Processor

MODEL_ID = os.environ.get("SAM_MODEL", "facebook/sam3.1")
DEFAULT_PROMPT = os.environ.get("SAM_PROMPT", "person")
DEFAULT_THRESHOLD = float(os.environ.get("SAM_THRESHOLD", "0.5"))

if torch.cuda.is_available():
    device = "cuda"
elif torch.backends.mps.is_available():
    device = "mps"
else:
    device = "cpu"

processor = Sam3Processor.from_pretrained(MODEL_ID)
model = Sam3Model.from_pretrained(MODEL_ID).to(device).eval()

app = FastAPI(title="SAM segmentation server")
lock = threading.Lock()  # one inference at a time on the GPU


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_ID, "device": device, "prompt": DEFAULT_PROMPT}


def run_sam(image: Image.Image, prompt: str, threshold: float):
    inputs = processor(images=image, text=prompt, return_tensors="pt").to(device)
    with lock, torch.inference_mode():
        outputs = model(**inputs)
    return processor.post_process_instance_segmentation(
        outputs,
        threshold=threshold,
        mask_threshold=0.5,
        target_sizes=[(image.height, image.width)],
    )[0]



@app.post("/segment")
async def segment(request: Request, prompt: str = DEFAULT_PROMPT, threshold: float = DEFAULT_THRESHOLD):
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body; send raw JPEG bytes")
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "could not decode image")

    result = await run_in_threadpool(run_sam, image, prompt, threshold)
    boxes = result["boxes"].float().cpu().tolist()
    scores = result["scores"].float().cpu().tolist()
    return {
        "width": image.width,
        "height": image.height,
        "prompt": prompt,
        "objects": [
            {"id": i, "score": round(s, 4), "box": [round(v, 1) for v in b]}
            for i, (b, s) in enumerate(zip(boxes, scores))
        ],
    }


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")))
