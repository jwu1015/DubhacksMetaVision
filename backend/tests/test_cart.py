"""pytest tests/test_cart.py  (from backend/). In-memory only; nothing is bought."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import cart


def product(title="Mid-Century Tripod Floor Lamp", store="West Elm", price=349.0, rank=0):
    return {"title": title, "store": store, "price": price, "url": f"https://shop.example/{rank}",
            "image_url": None, "source": "lens", "tier": "new", "source_rank": rank}


@pytest.fixture
def client():
    cart.items.clear()
    app = FastAPI()
    app.include_router(cart.router)
    return TestClient(app)


def test_empty_cart(client):
    assert client.get("/cart").json() == {"items": [], "total": 0.0, "spoken_summary": "Your cart is empty."}


def test_add_to_cart(client):
    body = client.post("/cart", json={"product": product()}).json()
    assert [p["title"] for p in body["items"]] == ["Mid-Century Tripod Floor Lamp"]
    assert body["total"] == 349.0
    assert body["spoken_summary"] == "Added the West Elm item, 349 dollars. Your cart has 1 item, 349 dollars total."


def test_total_is_computed_in_code(client):
    client.post("/cart", json={"product": product()})
    body = client.post("/cart", json={"product": product("Walnut Tripod Lamp", "Target", 89.99, 1)}).json()
    assert body["total"] == 438.99
    assert body["spoken_summary"].endswith("Your cart has 2 items, 439 dollars total.")
    assert client.get("/cart").json()["total"] == 438.99  # GET sees the same cart


def test_float_total_rounds_to_cents(client):
    for price in (0.1, 0.2):
        body = client.post("/cart", json={"product": product(price=price)}).json()
    assert body["total"] == 0.3  # not 0.30000000000000004


def test_remove_from_cart(client):
    client.post("/cart", json={"product": product()})
    client.post("/cart", json={"product": product("Walnut Tripod Lamp", "Target", 89.99, 1)})
    body = client.delete("/cart/0").json()
    assert [p["store"] for p in body["items"]] == ["Target"] and body["total"] == 89.99
    assert body["spoken_summary"].startswith("Removed the West Elm item.")


@pytest.mark.parametrize("i", [0, 5, -1])
def test_remove_missing_item_is_404(client, i):
    assert client.delete(f"/cart/{i}").status_code == 404


def test_store_less_product_reads_naturally(client):
    body = client.post("/cart", json={"product": product(store="", price=1.0)}).json()
    assert body["spoken_summary"].startswith("Added that item, 1 dollar.")


def test_bad_body_is_422(client):
    assert client.post("/cart", json={"title": "not wrapped in product"}).status_code == 422
