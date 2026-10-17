"""
Model service: SAM 3 (/segment) + CLIP (/embed) on one Modal GPU.

Dev (hot reload, temporary URL):  modal serve model_service/modal_app.py
Deploy (stable URL):              modal deploy model_service/modal_app.py

Secrets (create once):
  modal secret create huggingface HF_TOKEN=hf_xxx
  modal secret create gtl-auth MODEL_SERVICE_TOKEN=<random string, share with Person A>

Untested draft: SAM 3 output handling follows the repo README
(github.com/facebookresearch/sam3). Check it against
examples/sam3_image_predictor_example.ipynb on your first run.
"""
import modal

app = modal.App("gtl-models")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "libgl1", "libglib2.0-0")
    # SAM 3 README: Python 3.12+, PyTorch 2.7+, CUDA 12.6+
    .pip_install("torch==2.10.0", "torchvision", index_url="https://download.pytorch.org/whl/cu128")
    .run_commands(
        "git clone https://github.com/facebookresearch/sam3.git /opt/sam3",
        "pip install -e /opt/sam3",
    )
    .pip_install("open_clip_torch", "pillow", "numpy", "httpx", "fastapi[standard]")
)

# Weights (SAM 3 from HF, CLIP from HF) cache here so cold starts don't re-download.
hf_cache = modal.Volume.from_name("gtl-hf-cache", create_if_missing=True)

# Ship /embed first: keep False until Hugging Face approves facebook/sam3.
ENABLE_SAM = False

# Used when /segment gets no text prompt (lets SAM run in parallel with Claude).
HOME_GOODS = [
    "lamp", "chair", "sofa", "table", "rug", "shelf", "vase",
    "mirror", "bed", "cabinet", "plant pot", "pillow", "artwork",
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@app.cls(
    image=image,
    gpu="L4",
    secrets=[modal.Secret.from_name("huggingface"), modal.Secret.from_name("gtl-auth")],
    volumes={"/root/.cache/huggingface": hf_cache},
    min_containers=1,       # keep one warm during dev + demo; SET TO 0 AFTER THE EVENT
    scaledown_window=600,
    timeout=120,
)
class Models:
    @modal.enter()
    def load(self):
        import os
        import open_clip

        self.device = "cuda"
        self.token = os.environ["MODEL_SERVICE_TOKEN"]

        self.clip, _, self.clip_pre = open_clip.create_model_and_transforms(
            "ViT-B-32", pretrained="laion2b_s34b_b79k", device=self.device
        )
        self.clip.eval()

        self.sam = None
        if ENABLE_SAM:
            from sam3.model_builder import build_sam3_image_model
            from sam3.model.sam3_image_processor import Sam3Processor

            self.sam = Sam3Processor(build_sam3_image_model())

        hf_cache.commit()  # persist any weights downloaded on this start

    @modal.asgi_app()
    def api(self):
        import asyncio
        import base64
        import io
        import time

        import httpx
        import numpy as np
        import torch
        from fastapi import FastAPI, Header, HTTPException
        from PIL import Image
        from pydantic import BaseModel

        web = FastAPI()

        def check_auth(authorization: str | None):
            if authorization != f"Bearer {self.token}":
                raise HTTPException(status_code=401, detail="bad token")

        def b64_to_image(data: str) -> Image.Image:
            if data.startswith("data:"):
                data = data.split(",", 1)[1]
            return Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")

        def mask_to_png_b64(mask: np.ndarray) -> str:
            buf = io.BytesIO()
            Image.fromarray((mask * 255).astype(np.uint8), mode="L").save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()

        def iou(a: dict, b: dict) -> float:
            ax2, ay2, bx2, by2 = a["x"] + a["w"], a["y"] + a["h"], b["x"] + b["w"], b["y"] + b["h"]
            iw = max(0.0, min(ax2, bx2) - max(a["x"], b["x"]))
            ih = max(0.0, min(ay2, by2) - max(a["y"], b["y"]))
            inter = iw * ih
            union = a["w"] * a["h"] + b["w"] * b["h"] - inter
            return inter / union if union > 0 else 0.0

        # ---------- /health ----------
        @web.get("/health")
        def health():
            return {
                "ok": True,
                "sam_loaded": self.sam is not None,
                "clip_loaded": self.clip is not None,
                "device": self.device,
            }

        # ---------- /segment ----------
        class Box(BaseModel):
            x: float
            y: float
            w: float
            h: float

        class SegmentReq(BaseModel):
            image_b64: str
            box: Box | None = None   # used to RANK masks by overlap, not as a SAM prompt
            text: str | None = None  # missing -> run the HOME_GOODS list

        @web.post("/segment")
        def segment(req: SegmentReq, authorization: str | None = Header(None)):
            check_auth(authorization)
            if self.sam is None:
                raise HTTPException(status_code=503, detail="SAM not loaded")
            t0 = time.time()

            img = b64_to_image(req.image_b64)
            W, H = img.size
            prompts = [req.text] if req.text else HOME_GOODS

            candidates = []
            with torch.inference_mode():
                state = self.sam.set_image(img)  # image encoder runs once
                for prompt in prompts:
                    if hasattr(self.sam, "reset_all_prompts"):
                        self.sam.reset_all_prompts(state)
                    out = self.sam.set_text_prompt(state=state, prompt=prompt)
                    for m, b, s in zip(out["masks"], out["boxes"], out["scores"]):
                        mask = m.squeeze().detach().cpu().numpy() > 0.5
                        x1, y1, x2, y2 = [float(v) for v in b.tolist()]  # pixel xyxy
                        candidates.append({
                            "label": prompt,
                            "score": float(s),
                            "mask": mask,
                            "bbox": {"x": x1 / W, "y": y1 / H, "w": (x2 - x1) / W, "h": (y2 - y1) / H},
                        })

            if req.box:
                target = req.box.model_dump()
                for c in candidates:
                    c["iou"] = iou(c["bbox"], target)
                candidates.sort(key=lambda c: (c["iou"] > 0.3, c["iou"], c["score"]), reverse=True)
            else:
                candidates.sort(key=lambda c: c["score"], reverse=True)

            masks = [
                {
                    "score": round(c["score"], 4),
                    "label": c["label"],
                    "bbox": {k: round(v, 4) for k, v in c["bbox"].items()},
                    "mask_png_b64": mask_to_png_b64(c["mask"]),
                }
                for c in candidates[:3]
            ]
            return {"masks": masks, "latency_ms": int((time.time() - t0) * 1000)}

        # ---------- /embed ----------
        class EmbedReq(BaseModel):
            query_url: str | None = None
            query_b64: str | None = None  # preferred: avoids downloading through ngrok
            candidate_urls: list[str]

        async def fetch(client: httpx.AsyncClient, url: str):
            try:
                if url.startswith("data:"):
                    return b64_to_image(url)
                r = await client.get(url, timeout=2.0)
                r.raise_for_status()
                return Image.open(io.BytesIO(r.content)).convert("RGB")
            except Exception:
                return None

        @web.post("/embed")
        async def embed(req: EmbedReq, authorization: str | None = Header(None)):
            check_auth(authorization)
            t0 = time.time()

            async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True) as client:
                if req.query_b64:
                    query = b64_to_image(req.query_b64)
                elif req.query_url:
                    query = await fetch(client, req.query_url)
                else:
                    raise HTTPException(status_code=400, detail="need query_b64 or query_url")
                cands = await asyncio.gather(*[fetch(client, u) for u in req.candidate_urls])

            if query is None:
                raise HTTPException(status_code=400, detail="could not load query image")

            images = [query] + [c for c in cands if c is not None]
            with torch.inference_mode():
                batch = torch.stack([self.clip_pre(i) for i in images]).to(self.device)
                feats = self.clip.encode_image(batch)
                feats = feats / feats.norm(dim=-1, keepdim=True)
            sims = iter((feats[1:] @ feats[0]).tolist())

            similarities = [round(next(sims), 4) if c is not None else None for c in cands]
            return {"similarities": similarities, "latency_ms": int((time.time() - t0) * 1000)}

        return web
