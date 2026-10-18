"""What the user is looking at, as the voice agent knows it: from the backend's messages in the room
(topic "updates": data packets, and text streams for the "look" messages)."""
import asyncio
import json
import logging
import math
import time

from livekit import rtc

from storefront.rtc.tokens import UPDATES, VISION
from storefront.voice.resolve import Resolved, distance, in_view, middle, resolve

log = logging.getLogger("storefront.voice")

NEAR = 0.15    # in front of the camera: an object whose box comes this close to the center of the view
PICK_S = 60.0  # a tap in the app is what "this" means for this long, while its object is still tracked
AIM_R = 0.5    # a box whose middle is this far from the center of the view isn't aimed at at all
HOLD_S = 1.0   # aiming counts over about this long: what they keep pointing at beats what passes by
MIN_AREA = 0.003  # boxes under this share of the view (about 37x37 px of the phone's 504x896) come last
AMBIGUOUS = 0.08  # two objects in front that score this close: the agent asks which one they mean
LEFT_OUT_S = 60.0  # an item the user said is the wrong one isn't "this" for this long, unless they pick it again


def aim(o: dict) -> float:
    """How squarely the camera points at an object: 1 when its box's middle is the center of the view, down to
    0 at AIM_R away. A box covering the center doesn't make it aimed at: that's often something in front of or
    behind what they mean."""
    x, y = middle(o)
    return max(0.0, 1.0 - math.hypot(x - 0.5, y - 0.5) / AIM_R)


def area(o: dict) -> float:
    x1, y1, x2, y2 = o["box"]
    return (x2 - x1) * (y2 - y1)


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
        self.held: dict[str, float] = {}     # uid -> its aim, averaged over about the last HOLD_S seconds
        self.held_at: float | None = None    # when the held aims were last brought up to date, by self.clock
        self.left_out: dict[str, float] = {}  # uid -> when the user said it's not the item they mean
        self.ranked: list[str] = []          # the uids the last resolve found, best first
        self.tried: list[str] = []           # those looked at since, the latest last
        self.asked: list[str] = []           # the two its question was about, until it's answered

    def forget(self):
        """Tracking restarted (a mode switch, a pause, a new category): the old uids mean nothing now."""
        self.objects, self.labels, self.looks, self.described = [], {}, {}, {}
        self.ranked, self.tried, self.asked = [], [], []
        self.picked, self.agent_picked, self.left_out = None, set(), {}
        self.held, self.held_at = {}, None
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
            self.hold()
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
                    self.left_out.pop(uid, None)

    def hold(self):
        """Fold the latest objects into each one's held aim: its aim, averaged over about the last HOLD_S."""
        now = self.clock()
        k = 1 - math.exp(-(now - self.held_at) / HOLD_S) if self.held_at is not None else 0.0
        self.held_at = now
        aims = {o["uid"]: aim(o) if in_view(o) else 0.0 for o in self.objects}
        for uid in self.held.keys() | aims.keys():
            h = self.held.get(uid, 0.0)
            self.held[uid] = h + (aims.get(uid, 0.0) - h) * k

    def score(self, o: dict) -> float:
        """How surely the user means this object in front: how squarely the camera points at it now, mostly, and
        over the last second or so, then a little SAM's confidence. Not its size."""
        return 0.6 * aim(o) + 0.4 * self.held.get(o["uid"], 0.0) + 0.1 * o.get("score", 0.0)

    def in_front(self) -> list[dict]:
        """The objects in front of the camera, those in view whose box covers the center of the view or comes
        within NEAR of it and that the user hasn't just said are the wrong one, best first: by score, boxes under
        MIN_AREA last."""
        near = [o for o in self.objects if in_view(o) and distance(o) <= NEAR and not self.is_left_out(o["uid"])]
        return sorted(near, key=lambda o: (area(o) < MIN_AREA, -self.score(o)))

    def current(self) -> dict | None:
        """The object in front of the camera (the best of in_front), or None."""
        return next(iter(self.in_front()), None)

    def rival(self, o: dict) -> dict | None:
        """Another object in front the user may just as well mean: one scoring within AMBIGUOUS of o."""
        return next((r for r in self.in_front() if r["uid"] != o["uid"] and area(r) >= MIN_AREA
                     and self.score(o) - self.score(r) < AMBIGUOUS), None)

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
        self.left_out.pop(uid, None)

    def leave_out(self, uid: str):
        """The user said this isn't the item they mean: it isn't "this" for LEFT_OUT_S, unless they pick it again."""
        self.left_out[uid] = self.clock()
        if self.picked == uid:
            self.picked = None

    def is_left_out(self, uid: str) -> bool:
        t = self.left_out.get(uid)
        return t is not None and self.clock() - t <= LEFT_OUT_S

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
