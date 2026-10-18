"""Shared contracts (spec Section 4a). Change only after agreeing with the team.

Additions beyond the spec are marked PROPOSED and are all optional/additive.
"""
from typing import Literal

from pydantic import BaseModel


class BBox(BaseModel):            # normalized 0-1, x/y = top-left
    x: float
    y: float
    w: float
    h: float


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


class Segmentation(BaseModel):
    method: Literal["sam3", "roboflow_sam3", "bbox_fallback"]
    bbox: BBox                        # tight box from the mask (or padded Claude box on fallback)
    mask_score: float | None = None
    cutout_path: str                  # item on white background, cropped
    cutout_url: str
    mask_preview_url: str | None = None   # original with mask overlay, for debugging/app
    latency_ms: int


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


class Capture(BaseModel):
    capture_id: str
    image_path: str
    image_url: str
    item: Item
    segmentation: Segmentation | None
    image_sha256: str | None = None   # PROPOSED: lets /search look up the demo cache


class DetectResponse(BaseModel):
    capture_id: str
    image_url: str
    item: Item
    segmentation: Segmentation | None
    quick_phrase: str
    latency_ms: int


class SearchResponse(BaseModel):
    capture_id: str
    best_match: Product | None
    dupes: list[Product]
    used: list[Product]
    price_insight: PriceInsight
    spoken_summary: str
    timing_ms: dict[str, int]
    item: Item | None = None          # ADDED: the finished item (Claude's name, description, queries)


class IdentifyResponse(BaseModel):   # PROPOSED: shape of "/detect + /search merged"
    detect: DetectResponse
    search: SearchResponse | None     # None when no item was detected


# --- Request/response bodies for the remaining routes (typed versions of Section 4e) ---

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


class HealthResponse(BaseModel):
    ok: bool
    model_service: bool
    fixtures: bool
    demo_mode: bool
