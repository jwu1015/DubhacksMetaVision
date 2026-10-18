"""Shared contracts: the REST bodies, and the live messages the phone and the projector page get.
Mirrored in the app's Models.swift; change them together."""
from typing import Literal

from pydantic import BaseModel


class BBox(BaseModel):            # normalized 0-1, x/y = top-left
    x: float
    y: float
    w: float
    h: float

    def corners(self) -> list[float]:
        """[x1, y1, x2, y2], as the live messages send boxes."""
        return [self.x, self.y, min(round(self.x + self.w, 4), 1.0), min(round(self.y + self.h, 4), 1.0)]


class Item(BaseModel):
    category: str | None
    description: str | None = None
    style: str | None = None
    colors: list[str] = []
    materials: list[str] = []
    brand_guess: str | None = None
    confidence: float = 0.0
    search_queries: list[str] = []
    bbox: BBox | None = None          # rough box from Claude
    quick_phrase: str = ""


class Product(BaseModel):
    title: str
    store: str
    price: float
    currency: str = "USD"
    url: str
    image_url: str | None = None
    source: Literal["lens", "shopping", "ebay", "amazon"]
    tier: Literal["new", "dupe", "used"] = "new"
    source_rank: int
    match_score: int | None = None     # 0-100, set by rerank
    savings_pct: int | None = None     # set by pricing
    match_reason: str | None = None    # set by rank


class PriceInsight(BaseModel):
    typical_low: float | None
    typical_high: float | None
    median: float | None
    sample_size: int
    best_match_verdict: Literal["low", "fair", "high"]
    verdict_text: str


class SearchResponse(BaseModel):
    capture_id: str                   # the Look's id (named for the app's current decoder)
    best_match: Product | None
    dupes: list[Product]
    used: list[Product]
    price_insight: PriceInsight
    spoken_summary: str
    timing_ms: dict[str, int]
    item: Item | None = None          # the finished item (Claude's name, description, queries)


# ---------- live messages: backend -> phone and projector ----------

Corners = list[float]  # [x1, y1, x2, y2], normalized 0-1


class LiveObject(BaseModel):
    uid: str                          # never repeats within a session: "<tracking session>.<SAM id>"
    box: Corners                      # the last one seen
    prompt: str                       # SAM's prompt that matched, e.g. "lamp"
    score: float
    in_view: bool                     # false: left the view in the last 15 s; not a hotspot
    last_seen_ts: int                 # ms since epoch


class ObjectsMessage(BaseModel):
    type: Literal["objects"] = "objects"
    session: int                      # goes up when tracking restarts and uids start over
    age_ms: int                       # how old the frame behind these boxes is
    objects: list[LiveObject]         # in view first, in the tracker's order, then the rest


class LabelMessage(BaseModel):
    type: Literal["label"] = "label"
    uid: str
    box: Corners
    name: str                         # e.g. "black insulated water bottle"
    price_low: int                    # whole dollars, a rough estimate
    price_high: int


LookStage = Literal["started", "described", "results", "failed"]


class LookMessage(BaseModel):
    type: Literal["look"] = "look"
    uid: str
    look_id: str
    stage: LookStage
    label: str                        # SAM's prompt, until Claude's item names it
    box: Corners
    selected: bool = False            # the user picked this object
    crop_url: str | None = None       # the crop, once the user picked the object
    item: Item | None = None          # from "described" on
    search: SearchResponse | None = None  # "results"
    message: str | None = None        # "failed": why, in a short sentence


class StatusMessage(BaseModel):
    type: Literal["status"] = "status"
    tracking: Literal["stopped", "starting", "ok", "offline"]
    category: str                     # what the prompts were picked within
    prompts: list[str]                # what SAM tracks; empty until the first frame


# ---------- REST bodies ----------

class AddToCartRequest(BaseModel):
    product: Product


class Cart(BaseModel):
    items: list[Product]
    total: float
    spoken_summary: str


class CheckoutItem(BaseModel):
    title: str
    price: float
    url: str


class CheckoutRequest(BaseModel):
    items: list[CheckoutItem]


class CheckoutResponse(BaseModel):
    checkout_url: str


class VoiceTokenRequest(BaseModel):   # LiveKit's standard token-endpoint request; every field is optional
    participant_identity: str | None = None
    participant_name: str | None = None


class VoiceTokenResponse(BaseModel):  # ... and its response, which LiveKit's client SDKs read
    server_url: str
    participant_token: str


class HealthResponse(BaseModel):
    ok: bool
    model_service: bool
    webrtc_sessions: int = 0     # phones streaming over WebRTC (one at a time)
    scene_memory: bool = False   # the live stream is tracking, so scene memory is filling
    voice: bool = False          # the voice assistant is set up (LiveKit keys present)
