"""What the user is looking at, as the voice agent knows it: from the backend's messages in the room
(topic "updates": data packets, and text streams for the "look" messages)."""
import asyncio
import json
import logging
import time

from livekit import rtc

from storefront.rtc.tokens import UPDATES, VISION
from storefront.voice.resolve import Resolved, distance, in_view, resolve

log = logging.getLogger("storefront.voice")

NEAR = 0.15    # in front of the camera: an object whose box comes this close to the center of the view
PICK_S = 60.0  # a tap in the app is what "this" means for this long, while its object is still tracked


class Seen:
    """The scene memory (the tracked objects in view, then those seen in the last few seconds), what Claude
    made of each object a Look described, and the item the user picked last. Also what the assistant last
    made of "that lamp", for "no, the other one" and the answers to its questions."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.session: int | None = None      # the tracking session the uids belong to
        self.objects: list[dict] = []        # the "objects" message's objects, in view first
        self.prompts: list[str] = []         # what SAM tracks this session
        self.labels: dict[str, dict] = {}    # uid -> its quick label: {"name", "price_low", "price_high"}
        self.looks: dict[str, dict] = {}     # uid -> its Look's latest "look" message
        self.described: dict[str, str] = {}  # uid -> Claude's name and description, from its Look
        self.look: dict | None = None        # the latest "look" message of the object the user picked
        self.picked: str | None = None       # the uid the user last tapped in the app, since the agent joined
        self.picked_at = 0.0                 # when, by self.clock
        self.agent_picked: set[str] = set()  # uids the assistant picked itself, to start their Looks: not taps
        self.ranked: list[str] = []          # the uids the last resolve found, best first
        self.tried: list[str] = []           # those looked at since, the latest last
        self.asked: list[str] = []           # the two its question was about, until it's answered

    def forget(self):
        """Tracking restarted (a mode switch, a pause, a new category): the old uids mean nothing now."""
        self.objects, self.labels, self.looks, self.described = [], {}, {}, {}
        self.ranked, self.tried, self.asked = [], [], []
        self.picked, self.agent_picked = None, set()
        if self.look:
            self.look = {**self.look, "uid": None}

    def update(self, m: dict):
        """Take one stream message."""
        kind = m.get("type")
        if kind == "objects":
            # A new session number means tracking restarted and uids start over. An empty view isn't a restart:
            # what was learned about its objects stays, for when they're back in view.
            if self.session is not None and m.get("session") != self.session:
                self.forget()
            self.session = m.get("session")
            self.objects = m["objects"]
        elif kind == "status":
            self.prompts = m["prompts"]
            if m["tracking"] == "stopped":
                self.forget()
        elif kind == "label":
            self.labels[m["uid"]] = {k: m[k] for k in ("name", "price_low", "price_high")}
        elif kind == "look":
            uid, before = m["uid"], self.looks.get(m["uid"])
            self.looks[uid] = m
            if m.get("item"):
                self.described[uid] = " ".join(filter(None, (m["item"].get("category"), m["item"].get("description"))))
            if m.get("selected"):
                self.look = m
                # A tap shows up as its Look turning selected. A Look that arrives already selected was picked
                # before the agent joined (the catch-up when it does), and the assistant's own picks aren't taps.
                if before is not None and not before.get("selected") and uid not in self.agent_picked:
                    self.picked, self.picked_at = uid, self.clock()

    def current(self) -> dict | None:
        """The object in front of the camera: of those in view whose box covers the center of the view or comes
        within NEAR of it, the nearest (the smallest when several cover it: a mug in front of a sofa). None when
        none is."""
        def key(o: dict) -> tuple[float, float]:
            x1, y1, x2, y2 = o["box"]
            return distance(o), (x2 - x1) * (y2 - y1)
        return min((o for o in self.objects if in_view(o) and distance(o) <= NEAR), key=key, default=None)

    def focus(self) -> tuple[dict | None, str]:
        """What "this" is: the object the user tapped in the app in the last PICK_S seconds, while it's still
        tracked (in view, or seen in the last few seconds); else the one in front of the camera. Returns (the
        object, "picked" | "in_front"), or (None, "none")."""
        if self.picked and self.clock() - self.picked_at <= PICK_S:
            tapped = next((o for o in self.objects if o["uid"] == self.picked), None)
            if tapped is not None:
                return tapped, "picked"
        if (o := self.current()) is not None:
            return o, "in_front"
        return None, "none"

    def pick(self, uid: str):
        """The user picked this object by voice: it's what "this" means now, as a tap makes it."""
        self.picked, self.picked_at = uid, self.clock()

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
