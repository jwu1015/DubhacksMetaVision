"""Hit every endpoint of a running backend and check each response matches models.py.

    python smoke_test.py [base_url] [image]
    python smoke_test.py https://<subdomain>.ngrok-free.app     # also proves Lens can fetch our images

Defaults: http://localhost:8000 and fixtures/lamp_room.jpg.
"""
import sys
from pathlib import Path

import httpx

from models import CheckoutResponse, DetectResponse, HealthResponse, IdentifyResponse, SearchResponse

HERE = Path(__file__).resolve().parent
failures = 0


def step(name, fn):
    global failures
    try:
        print(f"PASS {name}: {fn()}")
    except Exception as e:  # report and keep going
        failures += 1
        print(f"FAIL {name}: {e}")


def main():
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
    image = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "fixtures" / "lamp_room.jpg"
    files = lambda: {"image": (image.name, image.read_bytes(), "image/jpeg")}  # noqa: E731
    c = httpx.Client(base_url=base, timeout=60, headers={"ngrok-skip-browser-warning": "1"})
    state = {}

    def health():
        h = HealthResponse.model_validate(c.get("/health").raise_for_status().json())
        return f"fixtures={h.fixtures} demo_mode={h.demo_mode} model_service={h.model_service}"

    def detect():
        r = c.post("/detect", files=files()).raise_for_status()
        d = state["detect"] = DetectResponse.model_validate(r.json())
        return f"{d.capture_id} category={d.item.category!r} '{d.quick_phrase}' {d.latency_ms}ms"

    def images():
        d = state["detect"]
        urls = [d.image_url] + ([d.segmentation.cutout_url, d.segmentation.mask_preview_url]
                                if d.segmentation else [])
        for u in filter(None, urls):
            if not u.startswith(base):
                raise ValueError(f"{u} doesn't start with {base} (is PUBLIC_BASE_URL set to this URL?)")
            # Fetch like Lens / the model service would: browser-ish UA, no ngrok skip header.
            r = httpx.get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).raise_for_status()
            if not r.headers.get("content-type", "").startswith("image/"):
                raise ValueError(f"{u} returned {r.headers.get('content-type')}, not an image "
                                 "(ngrok warning page? outside services can't fetch our images)")
        return f"{len(urls)} image URLs load for outside fetchers"

    def search():
        r = c.post(f"/search/{state['detect'].capture_id}").raise_for_status()
        s = state["search"] = SearchResponse.model_validate(r.json())
        best = f"{s.best_match.store} ${s.best_match.price}" if s.best_match else None
        return f"best={best} dupes={len(s.dupes)} used={len(s.used)} timing={s.timing_ms}"

    def unknown_id():
        code = c.post("/search/c_doesnotexist").status_code
        if code != 404:
            raise ValueError(f"expected 404, got {code}")
        return "404"

    def bad_image():
        code = c.post("/detect", files={"image": ("x.jpg", b"not an image", "image/jpeg")}).status_code
        if code != 400:
            raise ValueError(f"expected 400, got {code}")
        return "400"

    def identify():
        i = IdentifyResponse.model_validate(c.post("/identify", files=files()).raise_for_status().json())
        return f"{i.detect.capture_id} has_search={i.search is not None}"

    def checkout():
        s = state["search"]
        picks = [p for p in [s.best_match, *s.dupes] if p][:1]
        body = {"items": [{"title": p.title, "price": p.price, "url": p.url} for p in picks]}
        co = CheckoutResponse.model_validate(c.post("/checkout", json=body).raise_for_status().json())
        return co.checkout_url

    for name, fn in [("health", health), ("detect", detect), ("image urls", images), ("search", search),
                     ("search unknown id", unknown_id), ("detect bad image", bad_image),
                     ("identify", identify), ("checkout", checkout)]:
        step(name, fn)
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
