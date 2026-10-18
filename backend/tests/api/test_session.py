"""Tests for the token routes (api/session.py) and the projector page. No LiveKit server: the rooms are faked."""
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from livekit import api

from storefront import app as storefront_app
from storefront import config

KEY, SECRET = "APItestkey", "test-secret-" + "s" * 32


class FakeRooms:
    def __init__(self):
        self.current = None
        self.opened: list = []
        self.voice_rooms: list[str] = []

    async def open(self, category=None):
        self.opened.append(category)
        self.current = SimpleNamespace(name=f"storefront-{len(self.opened)}")
        return self.current.name

    async def open_voice(self):
        self.voice_rooms.append(f"storefront-voice-{len(self.voice_rooms) + 1}")
        return self.voice_rooms[-1]


@pytest.fixture
def client(monkeypatch):
    rooms = FakeRooms()
    monkeypatch.setattr(storefront_app.app.state, "rooms", rooms)
    with mock.patch.multiple(config, LIVEKIT_URL="wss://test.livekit.cloud", LIVEKIT_API_KEY=KEY,
                             LIVEKIT_API_SECRET=SECRET):
        yield TestClient(storefront_app.app), rooms  # no lifespan: no warm-up, no segmentation server


def claims(token: str):
    return api.TokenVerifier(KEY, SECRET).verify(token)


def test_a_session_token_opens_a_room_for_the_category(client):
    c, rooms = client
    r = c.post("/session/token", json={"participant_identity": "shiven", "category": "kitchen utensils"})
    assert r.status_code == 201 and r.json()["server_url"] == "wss://test.livekit.cloud"
    phone = claims(r.json()["participant_token"])
    assert (phone.identity, phone.video.room, rooms.opened) == ("phone-shiven", "storefront-1", ["kitchen utensils"])
    assert phone.video.can_publish and phone.video.room_join
    other = claims(c.post("/session/token").json()["participant_token"])
    assert other.identity.startswith("phone-") and other.video.room == "storefront-2" and rooms.opened[1] is None


def test_a_viewer_token_watches_the_current_room_hidden(client):
    c, rooms = client
    assert c.get("/stream/viewer-token").status_code == 404  # no session yet
    c.post("/session/token")
    viewer = claims(c.get("/stream/viewer-token").json()["participant_token"])
    assert viewer.video.room == "storefront-1" and viewer.identity.startswith("viewer-")
    assert viewer.video.hidden and not viewer.video.can_publish and not viewer.video.can_publish_data


def test_the_legacy_voice_token_opens_a_voice_room(client):
    c, rooms = client
    r = c.post("/voice/token", json={"participant_identity": "phone-shiven", "room_name": "theirs"})
    voice = claims(r.json()["participant_token"])
    assert r.status_code == 201 and voice.identity == "phone-shiven"
    assert voice.video.room == rooms.voice_rooms[0] == "storefront-voice-1"  # ours, not the one asked for
    assert rooms.opened == []  # the session's room is left alone
    other = claims(c.post("/voice/token").json()["participant_token"])
    assert other.identity.startswith("phone-") and other.video.room == "storefront-voice-2"


def test_not_set_up(client):
    c, _ = client
    with mock.patch.object(config, "LIVEKIT_URL", ""):
        for method, path in (("post", "/session/token"), ("get", "/stream/viewer-token"), ("post", "/voice/token")):
            r = getattr(c, method)(path)
            assert r.status_code == 503 and "LIVEKIT_URL" in r.json()["detail"]


def test_the_projector_page(client):
    c, _ = client
    r = c.get("/stream/viewer")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    for needle in ("livekit-client@2.22.3", '"viewer-token"', 'id="look"'):
        assert needle in r.text
