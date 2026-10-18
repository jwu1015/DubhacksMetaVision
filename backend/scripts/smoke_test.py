"""Hit the HTTP routes of a running backend and check each response matches models.py.

    python scripts/smoke_test.py [base_url]       # default http://localhost:8000, from backend/
"""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storefront.config import FIXTURES  # noqa: E402
from storefront.models import Cart, CheckoutResponse, HealthResponse, Product  # noqa: E402

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
    c = httpx.Client(base_url=base, timeout=60, headers={"ngrok-skip-browser-warning": "1"})
    product = Product.model_validate(json.loads((FIXTURES / "products_ranked_lamp.json").read_text())[0])

    def health():
        h = HealthResponse.model_validate(c.get("/health").raise_for_status().json())
        return f"model_service={h.model_service} scene_memory={h.scene_memory} voice={h.voice}"

    def cart():
        body = {"product": product.model_dump(mode="json")}
        added = Cart.model_validate(c.post("/cart", json=body).raise_for_status().json())
        removed = Cart.model_validate(c.delete(f"/cart/{len(added.items) - 1}").raise_for_status().json())
        return f"added and removed: {len(added.items)} -> {len(removed.items)} items"

    def checkout():
        body = {"items": [{"title": product.title, "price": product.price, "url": product.url}]}
        return CheckoutResponse.model_validate(c.post("/checkout", json=body).raise_for_status().json()).checkout_url

    for name, fn in [("health", health), ("cart", cart), ("checkout", checkout)]:
        step(name, fn)
    print("all passed" if not failures else f"{failures} failed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
