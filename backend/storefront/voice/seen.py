"""What the user is looking at, as the voice agent knows it: from the main server's stream events."""
import asyncio
import json
import logging
import os
import time
from typing import Callable

import websockets

from storefront.voice.resolve import Resolved, resolve

log = logging.getLogger("storefront.voice")

BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")  # the main server, on this laptop
RECONNECT_S = 2.0


class Seen:
    """What the user is looking at, from the main server's stream events: the scene memory (the tracked
    objects in view, then those seen in the last MEMORY_S seconds) and the latest Look (the item picked,
    then Claude's description, then the prices). Also what the assistant last made of "that lamp", for
    "no, the other one" and the answers to its questions."""

    def __init__(self):
        self.objects: list[dict] = []        # the scene objects, in view first
        self.described: dict[int, str] = {}  # Claude's name and description of each object looked at
        self.look: dict | None = None  # the Look's "detected" or "nothing" event, plus item, search, failed
        self.look_at = 0.0             # time.monotonic() when that Look started
        self.ranked: list[int] = []    # the objects the last look_at found, best first
        self.tried: list[int] = []     # those looked at since, the latest last
        self.asked: list[int] = []     # the two its question was about, until it's answered

    def forget(self):
        """The scene memory is empty: nothing seen for a while, or it was reset (a mode switch or a pause)
        and its ids start over, so none of the old ones mean anything now."""
        self.objects, self.described, self.ranked, self.tried, self.asked = [], {}, [], [], []
        if self.look:
            self.look["track_id"] = None

    def update(self, ev: dict) -> str | None:
        """Take one stream event; return what to say about it, if anything. A replayed Look happened
        before this session, maybe long ago, so it's ignored: each conversation starts with nothing picked."""
        kind = ev.get("type")
        if kind == "objects":
            self.objects = ev["objects"]
            if not self.objects:
                self.forget()
        elif kind == "status" and ev["tracking"] == "stopped":
            self.forget()
        if kind != "look" or ev.get("replay"):
            return None
        stage = ev["stage"]
        if stage in ("detected", "nothing"):
            self.look, self.look_at = dict(ev), time.monotonic()
        elif self.look is None or ev["capture_id"] != self.look["capture_id"]:
            return None  # a late event of an earlier Look
        elif stage == "described":
            self.look["item"] = item = ev["item"]
            if self.look.get("track_id") is not None:
                self.described[self.look["track_id"]] = " ".join(
                    filter(None, (item.get("category"), item.get("description"))))
        elif stage == "results":
            self.look["search"] = ev["search"]
        elif stage == "failed":
            self.look["failed"] = ev.get("message") or "failed"
        return {"detected": ev.get("quick_phrase"), "nothing": ev.get("quick_phrase"),
                "results": (ev.get("search") or {}).get("spoken_summary"), "failed": ev.get("message")}.get(stage)

    def resolve(self, target: str, exclude=()) -> Resolved:
        return resolve(self.objects, target, exclude, self.asked, self.described)

    async def follow(self, say: Callable[[str], None]):
        """Keep up with the main server's stream events, reconnecting whenever it restarts, until cancelled."""
        url = "ws" + BACKEND.removeprefix("http") + "/stream/events"
        while True:
            try:
                async with websockets.connect(url, max_size=2**22) as ws:
                    log.info("voice: following %s", url)
                    async for raw in ws:
                        try:
                            text = self.update(json.loads(raw))
                        except (ValueError, KeyError, TypeError) as e:
                            log.warning("voice: skipped an event (%r)", e)
                            continue
                        if text:
                            say(text)
            except (OSError, websockets.WebSocketException) as e:
                log.warning("voice: no stream events from %s (%s); retrying in %ds", url, e, RECONNECT_S)
            await asyncio.sleep(RECONNECT_S)
