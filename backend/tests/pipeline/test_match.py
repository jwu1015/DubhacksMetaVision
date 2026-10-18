"""Tests for match.py. No network: downloads and the Claude call are faked."""
import asyncio
import io
import json

import pytest
from PIL import Image

import config
import match
from models import Item, Product

ITEM = Item(category="water bottle", description="Green Owala FreeSip 24 oz bottle with a white lid.")
CROP = "https://crop.example/c.jpg"


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "green").save(buf, "JPEG")
    return buf.getvalue()


def product(n: int, rank: int | None = None, photo: bool = True) -> Product:
    return Product(title=f"Bottle {n}", store="Store", price=20.0 + n, url=f"https://shop.example/{n}",
                   image_url=f"https://img.example/{n}.jpg" if photo else None, source="shopping",
                   source_rank=n if rank is None else rank)


def run(monkeypatch, products, reply=None, error=None, missing=(), seen=None):
    async def fetch(url):
        return None if url in missing else jpeg()

    async def call_model(content):
        if seen is not None:
            seen.append(content)
        if error:
            raise error
        return json.dumps(reply)

    monkeypatch.setattr(match, "fetch", fetch)
    monkeypatch.setattr(match, "call_model", call_model)
    return asyncio.run(match.score(CROP, ITEM, products))


def test_scores_go_to_the_listing_claude_numbered(monkeypatch):
    seen = []
    products = [product(0), product(1), product(2)]
    out = run(monkeypatch, products, {"scores": [{"id": 1, "score": 92}, {"id": 3, "score": 40}, {"id": 2, "score": 77}]},
              seen=seen)
    assert [p.match_score for p in out] == [92, 77, 40]
    assert [p.url for p in out] == [p.url for p in products]  # same order; rerank sorts
    assert ITEM.description in seen[0][0]["text"]  # Claude gets the description with the crop


def test_made_up_ids_are_ignored_and_scores_clamped(monkeypatch):
    out = run(monkeypatch, [product(0), product(1)],
              {"scores": [{"id": 0, "score": 50}, {"id": 1, "score": 150}, {"id": 9, "score": 80}]})
    assert [p.match_score for p in out] == [100, None]


def test_only_top_ranked_photos_are_sent(monkeypatch):
    seen = []
    products = [product(n, rank=20 - n) for n in range(20)] + [product(99, rank=0, photo=False)]
    run(monkeypatch, products, {"scores": []}, seen=seen)
    listings = [b["text"] for b in seen[0] if b["type"] == "text"][1:]
    assert len(listings) == match.MAX_CANDIDATES
    assert listings[0].startswith("Listing 1: Bottle 19")  # lowest source rank first; no photo, no look


@pytest.mark.parametrize("reply, error", [(None, RuntimeError("API down")), ({"wrong": "shape"}, None)])
def test_claude_failure_leaves_products_unscored(monkeypatch, reply, error):
    products = [product(0), product(1)]
    assert run(monkeypatch, products, reply, error) == products


def test_unreadable_crop_skips_claude(monkeypatch):
    seen = []
    products = [product(0)]
    assert run(monkeypatch, products, {"scores": [{"id": 1, "score": 90}]}, missing={CROP}, seen=seen) == products
    assert seen == []


def test_a_photo_that_fails_to_download_stays_unscored(monkeypatch):
    out = run(monkeypatch, [product(0), product(1)], {"scores": [{"id": 1, "score": 88}]},
              missing={"https://img.example/0.jpg"})
    assert [p.match_score for p in out] == [None, 88]  # listing 1 is product 1, the first photo that loaded


def test_no_photos_skips_claude(monkeypatch):
    seen = []
    products = [product(0, photo=False)]
    assert run(monkeypatch, products, {"scores": []}, seen=seen) == products
    assert seen == []


def test_a_crop_in_memory_is_used_as_is(monkeypatch):
    """A background Look passes its crop itself: nothing to read from disk or download."""
    seen = []
    crop, real_fetch = jpeg(), match.fetch

    async def fetch(url):
        return await real_fetch(url) if isinstance(url, bytes) else jpeg()  # the listings' photos are faked

    async def call_model(content):
        seen.append(content)
        return json.dumps({"scores": [{"id": 1, "score": 90}]})

    monkeypatch.setattr(match, "fetch", fetch)
    monkeypatch.setattr(match, "call_model", call_model)
    out = asyncio.run(match.score(crop, ITEM, [product(0)]))
    assert out[0].match_score == 90
    assert seen[0][1]["source"]["data"] == match.jpeg_b64(crop, match.CROP_EDGE)  # image 0 is that crop


def test_fetch_reads_our_uploads_from_disk_and_nothing_else(tmp_path, monkeypatch):
    class NoNetwork:
        async def get(self, url, timeout):
            raise AssertionError("tried to download")

    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "c_1_cutout.jpg").write_bytes(b"crop")
    (tmp_path / ".env").write_text("SECRET=1")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(match.net, "client", lambda: NoNetwork())
    base = config.PUBLIC_BASE_URL
    assert asyncio.run(match.fetch(f"{base}/uploads/c_1_cutout.jpg")) == b"crop"
    assert asyncio.run(match.fetch(f"{base}/uploads/../.env")) is None  # outside uploads: not read from disk
