"""Scene memory: what the user has been looking at, from the live stream's tracking (stream.py).

For each tracked object it keeps: its label and last box, whether it's in view now, how centered it was
while in view (time-weighted), how long it was in view, when it was first and last seen, and its best
frame (sharpest x biggest x most centered), so a Look on it works even after it left the view ("the
chair I just passed").

Frames live only here, in memory: a track and its frame go WINDOW_S after it was last seen, and
everything goes at once on clear() (a pause, a camera switch, the phone disconnecting).
"""
import math
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image

from storefront.live.tracker import TrackResult
from storefront.models import BBox, LiveObject

WINDOW_S = 15.0    # a track is forgotten this long after it was last seen
MAX_GAP_S = 1.0    # a stall between tracked frames counts as at most this much viewing time
FIRST_WEIGHT_S = 0.05  # how much a first sighting counts toward "centered", before time has passed
SHARP_EDGE = 128   # sharpness is measured on the box scaled to at most this many pixels


@dataclass
class Track:
    uid: str
    label: str
    score: float
    box: BBox                  # the last one seen, normalized to the frame
    first_seen: float          # time.time() of the frames
    last_seen: float
    in_view: bool = True
    seconds_viewed: float = 0.0
    centered_sum: float = 0.0  # sum of centeredness x time in view, for the time-weighted average
    weight: float = 0.0
    frame: Image.Image | None = None  # the best frame so far, and the box in it
    frame_box: BBox | None = None
    frame_value: float = -1.0

    @property
    def centered(self) -> float:
        return self.centered_sum / self.weight if self.weight else 0.0

    def live_object(self) -> LiveObject:
        """This object in an "objects" message."""
        return LiveObject(uid=self.uid, box=self.box.corners(), prompt=self.label, score=round(self.score, 3),
                          in_view=self.in_view, last_seen_ts=round(self.last_seen * 1000))


def centeredness(box: BBox) -> float:
    """1 when the box's center is the view's center, 0 when it's in a corner."""
    cx, cy = box.x + box.w / 2, box.y + box.h / 2
    return max(0.0, 1.0 - math.hypot(cx - 0.5, cy - 0.5) / math.hypot(0.5, 0.5))


def sharpness(image: Image.Image, box: BBox) -> float:
    """Variance of the Laplacian over the box, scaled down to SHARP_EDGE px: higher is sharper."""
    W, H = image.size
    crop = image.crop((round(box.x * W), round(box.y * H), round((box.x + box.w) * W), round((box.y + box.h) * H)))
    crop.thumbnail((SHARP_EDGE, SHARP_EDGE))
    g = np.asarray(crop.convert("L"), dtype=np.float32)
    if min(g.shape) < 3:
        return 0.0
    laplacian = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(laplacian.var())


class Scene:
    def __init__(self, window_s: float = WINDOW_S, clock=time.time):
        self.window_s, self.clock = window_s, clock
        self.tracks: dict[str, Track] = {}
        self._in_view: list[str] = []  # uids of the latest tracked frame, in the tracker's order
        self._last: float | None = None  # when the latest tracked frame was captured

    def update(self, result: TrackResult, frame: Image.Image):
        """Take one tracked frame: its objects are in view, every other track isn't any more."""
        t = result.captured_at
        dt = min(max(t - self._last, 0.0), MAX_GAP_S) if self._last is not None else 0.0
        self._last = t
        for o in result.objects:
            track = self.tracks.get(o.uid)
            if track is None:
                track = self.tracks[o.uid] = Track(o.uid, o.label, o.score, o.box, first_seen=t, last_seen=t)
            elif track.in_view:  # in view since the previous frame too
                track.seconds_viewed += dt
            track.label, track.score, track.box, track.last_seen, track.in_view = o.label, o.score, o.box, t, True
            c = centeredness(o.box)
            w = max(dt, FIRST_WEIGHT_S)
            track.centered_sum += c * w
            track.weight += w
            value = sharpness(frame, o.box) * o.box.w * o.box.h * c
            if value > track.frame_value:
                track.frame, track.frame_box, track.frame_value = frame, o.box, value
        self._in_view = [o.uid for o in result.objects]
        for track in self.tracks.values():
            if track.uid not in self._in_view:
                track.in_view = False
        self.expire(t)

    def expire(self, now: float | None = None):
        now = self.clock() if now is None else now
        for uid in [u for u, track in self.tracks.items() if now - track.last_seen > self.window_s]:
            del self.tracks[uid]

    def objects(self) -> list[LiveObject]:
        """Every remembered object: those in view first, in the tracker's order, then the rest, most
        recently seen first."""
        in_view = [self.tracks[u] for u in self._in_view if u in self.tracks]
        gone = sorted((t for t in self.tracks.values() if not t.in_view), key=lambda t: -t.last_seen)
        return [t.live_object() for t in in_view + gone]

    def best(self, uid: str) -> tuple[Image.Image, Track] | None:
        """A remembered track's best frame (with the track: its frame_box is the box in that frame)."""
        self.expire()
        track = self.tracks.get(uid)
        return (track.frame, track) if track and track.frame is not None else None

    def clear(self):
        self.tracks.clear()
        self._in_view, self._last = [], None
