"""Which scene object a spoken target means: "that lamp", "the green couch", "the chair I just passed"."""
import math
import re
import time
from dataclasses import dataclass
from itertools import takewhile

from storefront import config

MEMORY_S = 15         # the stream's scene memory keeps an object this long after it was last seen
CLOSE = 0.15          # two objects that fit what was said and score this close: ask which, don't guess

# What people call the tracker's labels (config.SAM_CATEGORIES). A desk is a table: SAM has no desk
# category, so a desk is tracked as a table.
SPOKEN = {
    "couch": "sofa", "loveseat": "sofa", "sectional": "sofa", "settee": "sofa",
    "armchair": "chair", "recliner": "chair", "stool": "chair",
    "desk": "table", "nightstand": "table", "light": "lamp", "carpet": "rug", "headboard": "bed",
    "bookcase": "shelf", "bookshelf": "shelf", "shelves": "shelf", "shelving": "shelf",
    "art": "wall art", "artwork": "wall art", "painting": "wall art", "picture": "wall art",
    "poster": "wall art", "print": "wall art", "frame": "wall art", "canvas": "wall art",
    "cushion": "pillow", "throw": "blanket", "quilt": "blanket",
}
# A noun after one of these isn't the one they mean: "the lamp next to the couch" is a lamp.
CUT = {"on", "in", "at", "by", "near", "next", "beside", "behind", "under", "over", "with", "from", "of",
       "that", "which", "i", "you", "we", "they"}
OTHER = {"other", "another", "different"}
HERE = {"this", "here", "front", "now"}   # in view: "the one in front of you"
PASSED = {"passed", "past", "last"}       # the most recently seen out of view: "the chair I just passed"
BEFORE = {"before", "earlier", "previous"}  # the one seen before that
SIDES = ((("left",), lambda o: middle(o)[0]), (("right",), lambda o: -middle(o)[0]),
         (("top", "upper", "higher"), lambda o: middle(o)[1]), (("bottom", "lower"), lambda o: -middle(o)[1]))
# Words that don't say what an object looks like (checked against Claude's descriptions: "the green one").
NOT_LOOKS = CUT | OTHER | HERE | PASSED | BEFORE | {w for side, _ in SIDES for w in side} | {
    "the", "these", "those", "one", "ones", "thing", "item", "and", "for", "your", "just", "there",
    "what", "how", "much", "about", "look", "like", "see", "saw", "seen", "was", "are", "its", "walked"}

# ---------- which object "that lamp" is ----------
# Scene objects come from the stream's "objects" events: {"id", "label", "score", "box": {"x","y","w","h"},
# "in_view", "centered", "seconds_viewed", "first_seen_ts", "last_seen_ts"}, in view first, then those seen
# in the last MEMORY_S seconds, most recent first. Events from before scene memory have only what's in
# view and none of its fields: an object then counts as in view, just now, centered as its box is.

def now_ms() -> float:
    return time.time() * 1000  # as the scene memory's timestamps


def in_view(o: dict) -> bool:
    return o.get("in_view", True)


def middle(o: dict) -> tuple[float, float]:
    b = o["box"]
    return b["x"] + b["w"] / 2, b["y"] + b["h"] / 2


def distance(o: dict) -> float:
    """From the center of the view to the object's box; 0 when the box covers it."""
    b = o["box"]
    dx = max(b["x"] - 0.5, 0.0, 0.5 - (b["x"] + b["w"]))
    dy = max(b["y"] - 0.5, 0.0, 0.5 - (b["y"] + b["h"]))
    return math.hypot(dx, dy)


def covers(o: dict) -> bool:
    return in_view(o) and distance(o) == 0


def score(o: dict, now: float) -> float:
    """How likely the user means this object, all else equal. Above 3 for one covering the center of the
    view now, the smaller the higher (a lamp in front of a sofa is the lamp); 1 to 2 for others in view,
    by how near the center they are now and were while in view; 0 to 1 for those out of view, by how
    recently they were seen."""
    if not in_view(o):
        return max(0.0, 1 - (now - o.get("last_seen_ts", now)) / 1000 / MEMORY_S)
    if covers(o):
        return 4 - o["box"]["w"] * o["box"]["h"]
    near = max(0.0, 1 - distance(o) / 0.5)
    return 1 + (near + o.get("centered", near)) / 2


def words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", text.lower())


def known_nouns(objects: list[dict]) -> dict[str, str]:
    """The tracker's labels by their last word ("art": "wall art"), each one a kind of object."""
    labels = [o["label"] for o in objects] + config.SAM_CATEGORIES.split(",")
    return {w[-1]: label.strip() for label in labels if (w := words(label))}


def noun(word: str, known: dict[str, str]) -> tuple[str, str] | None:
    """(the word, singular; the kind of object it means, as a label's last word) for a spoken word:
    "couches" is ("couch", "sofa"), "painting" ("painting", "art"). None if it's no kind we know."""
    for w in (word, word[:-1], word[:-2]) if word.endswith("s") else (word,):
        if w in SPOKEN:
            return w, words(SPOKEN[w])[-1]
        if w in known:
            return w, w
    return None


def kind_of(label: str, known: dict[str, str]) -> str:
    """The kind of object a tracker label is: a "floor lamp" is a lamp."""
    last = words(label)[-1:] or [label]
    found = noun(last[0], known)
    return found[1] if found else last[0]


def named(said: list[str], known: dict[str, str]) -> tuple[str, str] | None:
    """What kind of object the words name: the last noun before a preposition or a clause ("the lamp next
    to the couch" is a lamp, a "throw pillow" a pillow, "the chair I just passed" a chair). Returns
    (what to call it, its kind)."""
    head = said[:1] + list(takewhile(lambda w: w not in CUT, said[1:]))
    found = [n for n in (noun(w, known) for w in head) if n]
    if not found:
        return None
    word, kind = found[-1]
    return known.get(word, word), kind  # "No wall art...", not "No art..."


@dataclass
class Resolved:
    found: list[dict]       # the objects the user may mean, best first
    say: str | None = None  # instead of looking: a question when the best two are too close to call, or why none fit


def narrow(pool: list[dict], said: list[str], known: dict[str, str], described: dict[int, str]):
    """Apply the rest of what they said to the ranked objects: where it is ("on the left", "in front of
    you", "I just passed", "the one before") or what it looks like, by Claude's descriptions of those
    already looked at ("the green one"). Returns the objects, those that fit first, and True when that
    settled it."""
    cues = set(said) - ({"right"} if re.search(r"\bright (there|here|now)\b", " ".join(said)) else set())
    here, gone = [o for o in pool if in_view(o)], [o for o in pool if not in_view(o)]
    decided = False
    by = next((key for side, key in SIDES if cues & set(side)), None)
    if by and here:
        pool, decided = sorted(here, key=by), True
    elif cues & (PASSED | BEFORE) and gone:
        pool, decided = (gone[1:] if cues & BEFORE and len(gone) > 1 else gone), True
    elif cues & HERE and here:
        pool = here
    looks = {w for w in said if len(w) > 2 and w not in NOT_LOOKS and not noun(w, known)}
    fit = [o for o in pool if looks & set(words(described.get(o["id"], "")))]
    if fit:
        pool, decided = fit + [o for o in pool if o not in fit], True
    return pool, decided


def question(a: dict, b: dict) -> str:
    """A short question that tells two objects apart."""
    if a["label"] != b["label"]:
        return f"The {a['label']} or the {b['label']}?"
    label = a["label"]
    if in_view(a) and in_view(b):
        (ax, ay), (bx, by) = middle(a), middle(b)
        if abs(ax - bx) >= abs(ay - by):
            return f"The {label} on the left or the right?"
        return f"The higher {label} or the lower one?"
    if in_view(a) or in_view(b):
        return f"The {label} in front of you, or the one you passed?"
    return f"The {label} you just passed, or the one before?"


def resolve(objects: list[dict], target: str, exclude=(), among=(), described: dict[int, str] | None = None,
            now: float | None = None) -> Resolved:
    """The scene object a spoken target means. objects: the scene memory. target: what the user called
    it ("lamp", "the green couch", "the chair I just passed"), or "" for "that". exclude: track ids it
    isn't. among: the two objects the last question asked about, which a target without a noun ("the
    left one") then picks between. described: Claude's description of each object already looked at.

    The label first (what people call it counts: a couch is the sofa); then in view and at the center of
    it now; then the most recently seen; then the longest viewed. A target without a noun means what's in
    view. When the best two are too close to call, it asks which."""
    now = now_ms() if now is None else now
    known, said = known_nouns(objects), words(target)
    thing = named(said, known)
    fits = [o for o in objects if thing is None or kind_of(o["label"], known) == thing[1]]
    pool = [o for o in fits if o["id"] not in exclude]
    if thing is None:
        if any(o["id"] in among for o in pool):  # their answer to the last question
            pool = [o for o in pool if o["id"] in among]
        elif not set(said) & (PASSED | BEFORE):
            pool = [o for o in pool if in_view(o)]
    if not pool:
        if thing is None:
            return Resolved([], "Nothing else is in view." if exclude else "Nothing's in view right now.")
        if fits:
            return Resolved([], f"That's the only {thing[0]} I've seen.")
        return Resolved([], f"No {thing[0]} in view or seen recently.")
    pool.sort(key=lambda o: (-score(o, now), -o.get("seconds_viewed", 0)))
    pool, decided = narrow(pool, said, known, described or {})
    if (not decided and len(pool) > 1 and not covers(pool[1])
            and score(pool[0], now) - score(pool[1], now) < CLOSE):
        return Resolved(pool, question(pool[0], pool[1]))
    return Resolved(pool)
