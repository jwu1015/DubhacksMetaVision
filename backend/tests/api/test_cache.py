"""Tests for cache.py, in a temp cache folder. No network."""
import asyncio
import hashlib
import json

import httpx
import pytest

import cache
import config
import prewarm
from models import DetectResponse, SearchResponse

OLD, NEW = "https://old-tunnel.example", "https://new-tunnel.example"
SHA = "ab" * 32


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", OLD)
    monkeypatch.setattr(config, "DEMO_MODE", False)
    monkeypatch.setattr(cache, "DEMO_TIMEOUT_S", 0.05)


def detect(category="floor lamp") -> DetectResponse:
    text = (config.FIXTURES / "detect_response.json").read_text().replace("__BASE__", OLD)
    det = DetectResponse.model_validate_json(text)
    det.item.category = category
    return det


def search(best=True) -> SearchResponse:
    text = (config.FIXTURES / "search_response.json").read_text().replace("__BASE__", OLD)
    resp = SearchResponse.model_validate_json(text)
    if not best:
        resp.best_match = None
    return resp


def live(result=None, error=None, delay=0.0):
    async def make():
        await asyncio.sleep(delay)
        if error:
            raise error
        return result
    return make


def guard(kind, make):
    model = DetectResponse if kind == "detect" else SearchResponse
    return asyncio.run(cache.demo_guard(SHA, kind, make, model))


# ---------- storage ----------

def test_sha_is_sha256_of_bytes():
    assert cache.image_sha256(b"photo") == hashlib.sha256(b"photo").hexdigest()


def test_put_get_swaps_public_base_url(monkeypatch):
    cache.put(SHA, "detect", detect())
    on_disk = (cache.CACHE_DIR / SHA / "detect.json").read_text()
    assert "__BASE__/uploads/" in on_disk and OLD not in on_disk
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", NEW)  # the ngrok URL changed overnight
    got = cache.get(SHA, "detect")
    assert got["image_url"].startswith(f"{NEW}/uploads/")
    assert got["segmentation"]["cutout_url"].startswith(f"{NEW}/uploads/")


def test_get_missing_or_corrupt_is_none():
    assert cache.get(SHA, "search") is None
    path = cache.CACHE_DIR / SHA / "search.json"
    path.parent.mkdir(parents=True)
    path.write_text("{half a file")
    assert cache.get(SHA, "search") is None


# ---------- demo_guard ----------

def test_live_answers_are_saved_and_returned():
    assert guard("detect", live(detect())).item.category == "floor lamp"
    assert cache.get(SHA, "detect") is not None


def test_demo_mode_off_never_serves_saved():
    cache.put(SHA, "detect", detect())
    with pytest.raises(RuntimeError):
        guard("detect", live(error=RuntimeError("network down")))


@pytest.mark.parametrize("make", [live(error=RuntimeError("network down")), live(detect(), delay=5)],
                         ids=["error", "too slow"])
def test_demo_mode_serves_saved_on_error_or_timeout(monkeypatch, make):
    cache.put(SHA, "detect", detect("saved lamp"))
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert guard("detect", make).item.category == "saved lamp"


def test_demo_mode_without_saved_answer_reraises(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    with pytest.raises(RuntimeError):
        guard("detect", live(error=RuntimeError("network down")))


def test_demo_mode_without_saved_answer_waits_for_slow_live(monkeypatch):
    monkeypatch.setattr(config, "DEMO_MODE", True)  # timeout is 0.05s; live takes 0.2s
    assert guard("detect", live(detect(), delay=0.2)).item.category == "floor lamp"


@pytest.mark.parametrize("kind, empty, good", [
    ("detect", detect(category=None), detect("saved lamp")),
    ("search", search(best=False), search()),
])
def test_empty_live_answer_never_replaces_saved(monkeypatch, kind, empty, good):
    cache.put(SHA, kind, good)
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert guard(kind, live(empty)) == good  # served the saved answer
    monkeypatch.setattr(config, "DEMO_MODE", False)
    assert guard(kind, live(empty)) == empty  # live-only mode returns it as is...
    assert type(good).model_validate(cache.get(SHA, kind)) == good  # ...but never saves it


def test_good_live_answer_refreshes_saved(monkeypatch):
    cache.put(SHA, "detect", detect("old lamp"))
    monkeypatch.setattr(config, "DEMO_MODE", True)
    assert guard("detect", live(detect("new lamp"))).item.category == "new lamp"
    assert cache.get(SHA, "detect")["item"]["category"] == "new lamp"


# ---------- prewarm ----------

def test_prewarm_reports_failures_and_empty_answers(tmp_path, capsys):
    folder = tmp_path / "photos"
    folder.mkdir()
    for name in ["a_lamp.jpg", "b_broken.jpg", "c_wall.jpg", "notes.txt"]:
        (folder / name).write_bytes(name.encode())
    ok = {"detect": json.loads(detect().model_dump_json()), "search": json.loads(search().model_dump_json())}
    no_item = {"detect": json.loads(detect(category=None).model_dump_json()), "search": None}

    def handler(request: httpx.Request):
        body = request.content
        if b"b_broken" in body:
            return httpx.Response(400, json={"detail": "field 'image' must be a JPEG"})
        return httpx.Response(200, json=no_item if b"c_wall" in body else ok)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    failures = prewarm.prewarm(folder, "http://test", client)
    assert failures == [("b_broken.jpg", "HTTP 400: field 'image' must be a JPEG"),
                        ("c_wall.jpg", "no item found")]
    out = capsys.readouterr().out
    assert "West Elm $349.00" in out and "notes.txt" not in out  # non-photos are skipped
