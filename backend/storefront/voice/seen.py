"""What the user is looking at, as the voice agent knows it: from the main server's stream messages."""
import asyncio
import json
import logging
import os

import websockets

from storefront.voice.resolve import Resolved, resolve

log = logging.getLogger("storefront.voice")

BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")  # the main server, on this laptop
RECONNECT_S = 2.0


class Seen:
    """The scene memory (the tracked objects in view, then those seen in the last few seconds), what Claude
    made of each object a Look described, and the item the user picked last. Also what the assistant last
    made of "that lamp", for "no, the other one" and the answers to its questions."""

    def __init__(self):
        self.objects: list[dict] = []        # the "objects" message's objects, in view first
        self.prompts: list[str] = []         # what SAM tracks this session
        self.described: dict[str, str] = {}  # uid -> Claude's name and description, from its Look
        self.look: dict | None = None        # the latest "look" message of the object the user picked
        self.ranked: list[str] = []          # the uids the last resolve found, best first
        self.tried: list[str] = []           # those looked at since, the latest last
        self.asked: list[str] = []           # the two its question was about, until it's answered

    def forget(self):
        """Tracking restarted (a mode switch, a pause, a new category): the old uids mean nothing now."""
        self.objects, self.described, self.ranked, self.tried, self.asked = [], {}, [], [], []
        if self.look:
            self.look = {**self.look, "uid": None}

    def update(self, m: dict):
        """Take one stream message."""
        kind = m.get("type")
        if kind == "objects":
            self.objects = m["objects"]
            if not self.objects:
                self.forget()
        elif kind == "status":
            self.prompts = m["prompts"]
            if m["tracking"] == "stopped":
                self.forget()
        elif kind == "look":
            if m.get("item"):
                self.described[m["uid"]] = " ".join(filter(None, (m["item"].get("category"),
                                                                  m["item"].get("description"))))
            if m.get("selected"):
                self.look = m

    def resolve(self, target: str, exclude=()) -> Resolved:
        return resolve(self.objects, target, exclude, self.asked, self.described, self.prompts)

    async def follow(self):
        """Keep up with the main server's stream messages, reconnecting whenever it restarts, until cancelled."""
        url = "ws" + BACKEND.removeprefix("http") + "/stream/events"
        while True:
            try:
                async with websockets.connect(url, max_size=2**22) as ws:
                    log.info("voice: following %s", url)
                    async for raw in ws:
                        try:
                            self.update(json.loads(raw))
                        except (ValueError, KeyError, TypeError) as e:
                            log.warning("voice: skipped a message (%r)", e)
            except (OSError, websockets.WebSocketException) as e:
                log.warning("voice: no stream messages from %s (%s); retrying in %ds", url, e, RECONNECT_S)
            await asyncio.sleep(RECONNECT_S)
