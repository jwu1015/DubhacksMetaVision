"""A fake SAM tracking server (backend/segmentation/server.py's /session routes) with moving boxes, and a
synthetic room video for the WebRTC tests.

    python tests/fakes/tracking.py [--port 8003] [--latency 1.5]   # the fake server, for a manual run
"""
import argparse
import asyncio
import io
import math
import uuid
from pathlib import Path

import av
from fastapi import Body, FastAPI, HTTPException, Request
from PIL import Image

from storefront import config


def make_video(path, seconds: float = 1.0, fps: int = 15, size: tuple[int, int] = (320, 240)) -> Path:
    """A slow pan and zoom over fixtures/lamp_room.jpg that loops seamlessly, encoded with PyAV."""
    room = Image.open(config.FIXTURES / "lamp_room.jpg").convert("RGB")
    W, H = size
    n = round(seconds * fps)
    with av.open(str(path), "w") as out:
        video = out.add_stream("h264", rate=fps)
        video.width, video.height, video.pix_fmt = W, H, "yuv420p"
        for i in range(n):
            k = (1 - math.cos(2 * math.pi * i / n)) / 2  # 0 -> 1 -> 0
            cw = room.width / (1.15 + 0.3 * k)
            ch = min(cw * H / W, room.height)
            x, y = (room.width - cw) * (0.15 + 0.7 * k), (room.height - ch) / 2
            frame = room.crop((round(x), round(y), round(x + cw), round(y + ch))).resize(size)
            out.mux(video.encode(av.VideoFrame.from_image(frame)))
        out.mux(video.encode())
    return Path(path)


# ---------- the fake tracking server ----------

FAKE_OBJECTS = [  # id, label, score, box (normalized x, y, w, h)
    (1, "sofa", 0.91, (0.08, 0.55, 0.45, 0.35)),
    (2, "lamp", 0.84, (0.62, 0.12, 0.12, 0.55)),
    (3, "table", 0.77, (0.40, 0.72, 0.25, 0.18)),
]


def moving_boxes(i: int, W: int, H: int) -> list[dict]:
    """The fake objects on frame i, drifting sideways; the table drops out now and then."""
    dx = 0.06 * math.sin(i / 3)
    return [{"id": oid, "score": score, "prompt": label,
             "box": [round((x + dx) * W, 1), round(y * H, 1), round((x + dx + w) * W, 1), round((y + h) * H, 1)]}
            for oid, label, score, (x, y, w, h) in FAKE_OBJECTS if not (oid == 3 and i % 8 >= 5)]


def fake_server(latency: float = 0.0, max_sessions: int = 4) -> FastAPI:
    """Baptiste's tracking API with moving boxes. app.state.stats counts overlapping frames among others."""
    app = FastAPI(title="fake tracking server")
    sessions: dict[str, dict] = {}
    stats = {"created": 0, "frames": 0, "overlaps": 0, "deleted": 0}
    app.state.sessions, app.state.stats = sessions, stats

    @app.get("/health")
    async def health():
        return {"status": "ok", "model": "fake", "device": "cpu", "sessions": len(sessions)}

    @app.post("/session")
    async def create(body: dict = Body(default={})):
        if not body.get("prompt"):  # like the real server since 17 Oct
            raise HTTPException(400, 'prompt is required: "cup, laptop" or ["cup", "laptop"]')
        if len(sessions) >= max_sessions:
            raise HTTPException(503, f"too many sessions (max {max_sessions}); delete one or wait for expiry")
        sid = uuid.uuid4().hex
        sessions[sid] = {"frame_idx": 0, "busy": False}
        stats["created"] += 1
        return {"session_id": sid, "prompt": body.get("prompt", [o[1] for o in FAKE_OBJECTS])}

    @app.post("/session/{sid}/frame")
    async def frame(sid: str, request: Request):
        s = sessions.get(sid)
        if s is None:
            raise HTTPException(404, "unknown or expired session")
        stats["overlaps"] += s["busy"]
        s["busy"] = True
        try:
            img = Image.open(io.BytesIO(await request.body()))
            await asyncio.sleep(latency)
            i, s["frame_idx"] = s["frame_idx"], s["frame_idx"] + 1
            stats["frames"] += 1
            return {"frame_idx": i, "width": img.width, "height": img.height,
                    "objects": moving_boxes(i, img.width, img.height)}
        finally:
            s["busy"] = False

    @app.delete("/session/{sid}")
    async def delete(sid: str):
        if sessions.pop(sid, None) is None:
            raise HTTPException(404, "unknown or expired session")
        stats["deleted"] += 1
        return {"deleted": sid}

    return app


if __name__ == "__main__":
    import uvicorn
    p = argparse.ArgumentParser(description="Serve the fake tracking server.")
    p.add_argument("--port", type=int, default=8003)
    p.add_argument("--latency", type=float, default=1.5, help="seconds per frame")
    args = p.parse_args()
    uvicorn.run(fake_server(args.latency), host="127.0.0.1", port=args.port)
