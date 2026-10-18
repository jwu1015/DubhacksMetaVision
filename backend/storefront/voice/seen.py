"""What the user is looking at, as the voice agent knows it: from the backend's messages in the room
(topic "updates": data packets, and text streams for the "look" messages)."""
import asyncio
import json
import logging

from livekit import rtc

from storefront.rtc.tokens import UPDATES, VISION
from storefront.voice.resolve import Resolved, distance, in_view, resolve

log = logging.getLogger("storefront.voice")


class Seen:
    """The scene memory (the tracked objects in view, then those seen in the last few seconds), what Claude
    made of each object a Look described, and the item the user picked last. Also what the assistant last
    made of "that lamp", for "no, the other one" and the answers to its questions."""

    def __init__(self):
        self.objects: list[dict] = []        # the "objects" message's objects, in view first
        self.prompts: list[str] = []         # what SAM tracks this session
        self.labels: dict[str, dict] = {}    # uid -> its quick label: {"name", "price_low", "price_high"}
        self.looks: dict[str, dict] = {}     # uid -> its Look's latest "look" message
        self.described: dict[str, str] = {}  # uid -> Claude's name and description, from its Look
        self.look: dict | None = None        # the latest "look" message of the object the user picked
        self.ranked: list[str] = []          # the uids the last resolve found, best first
        self.tried: list[str] = []           # those looked at since, the latest last
        self.asked: list[str] = []           # the two its question was about, until it's answered

    def forget(self):
        """Tracking restarted (a mode switch, a pause, a new category): the old uids mean nothing now."""
        self.objects, self.labels, self.looks, self.described = [], {}, {}, {}
        self.ranked, self.tried, self.asked = [], [], []
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
        elif kind == "label":
            self.labels[m["uid"]] = {k: m[k] for k in ("name", "price_low", "price_high")}
        elif kind == "look":
            self.looks[m["uid"]] = m
            if m.get("item"):
                self.described[m["uid"]] = " ".join(filter(None, (m["item"].get("category"),
                                                                  m["item"].get("description"))))
            if m.get("selected"):
                self.look = m

    def current(self) -> dict | None:
        """The object the camera is on: of those in view, the one at the center of the view (the smallest when
        several cover it: a mug in front of a sofa), else the one nearest to it. None when nothing is in view."""
        def key(o: dict) -> tuple[float, float]:
            x1, y1, x2, y2 = o["box"]
            return distance(o), (x2 - x1) * (y2 - y1)
        return min((o for o in self.objects if in_view(o)), key=key, default=None)

    def resolve(self, target: str, exclude=()) -> Resolved:
        return resolve(self.objects, target, exclude, self.asked, self.described, self.prompts)

    def take(self, raw: str | bytes):
        """One message as it arrives; a malformed one is logged and skipped."""
        try:
            self.update(json.loads(raw))
        except (ValueError, KeyError, TypeError) as e:
            log.warning("voice: skipped a message (%r)", e)

    def listen(self, room: rtc.Room):
        """Keep up with the backend's messages in this room."""
        reading: set[asyncio.Task] = set()

        def on_data(packet: rtc.DataPacket):
            if packet.topic == UPDATES and packet.participant and packet.participant.identity == VISION:
                self.take(packet.data)

        async def read(reader: rtc.TextStreamReader):
            self.take(await reader.read_all())

        def on_text(reader: rtc.TextStreamReader, identity: str):
            if identity == VISION:
                task = asyncio.create_task(read(reader))
                reading.add(task)
                task.add_done_callback(reading.discard)

        room.on("data_received", on_data)
        room.register_text_stream_handler(UPDATES, on_text)
