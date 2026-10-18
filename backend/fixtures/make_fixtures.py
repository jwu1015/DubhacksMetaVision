"""Generate every fixture from one source of truth, then validate it against models.py.

    python fixtures/make_fixtures.py        (run from backend/)

URLs served by our backend use the placeholder __BASE__, which readers swap for PUBLIC_BASE_URL.
Product links point at example.com and thumbnails at placehold.co, so images load without fake store links.
"""
import json
import statistics
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from models import BBox, Item, PriceInsight, Product, SearchResponse  # noqa: E402

BASE = "__BASE__"
CAPTURE_ID = "c_fixture"
IMG_W, IMG_H = 1024, 768

# Where the lamp is drawn, and Claude's rougher box around it.
LAMP_BOX = BBox(x=0.33, y=0.14, w=0.22, h=0.71)
ROUGH = BBox(x=0.30, y=0.10, w=0.30, h=0.78)

WALNUT = (101, 67, 33)
LINEN = (238, 232, 218)


# ---------- images ----------

def draw_lamp(draw: ImageDraw.ImageDraw, box: BBox, fill_shade=LINEN, fill_wood=WALNUT):
    """Tripod floor lamp with a drum shade, filling `box` (normalized)."""
    x0, y0 = box.x * IMG_W, box.y * IMG_H
    w, h = box.w * IMG_W, box.h * IMG_H
    shade_h = h * 0.28
    draw.polygon(
        [(x0 + w * 0.2, y0), (x0 + w * 0.8, y0), (x0 + w, y0 + shade_h), (x0, y0 + shade_h)],
        fill=fill_shade,
    )
    hub = (x0 + w / 2, y0 + shade_h + h * 0.12)
    draw.line([(x0 + w / 2, y0 + shade_h), hub], fill=fill_wood, width=10)
    for foot_x in (x0 + 4, x0 + w / 2, x0 + w - 4):
        draw.line([hub, (foot_x, y0 + h)], fill=fill_wood, width=10)


def make_images():
    # Synthetic "room" photo: wall, floor, a rug, and the lamp.
    room = Image.new("RGB", (IMG_W, IMG_H), (205, 196, 180))
    d = ImageDraw.Draw(room)
    d.rectangle([0, int(IMG_H * 0.8), IMG_W, IMG_H], fill=(150, 120, 90))
    d.rectangle([80, int(IMG_H * 0.86), 900, IMG_H - 10], fill=(120, 60, 50))
    draw_lamp(d, LAMP_BOX)
    room.save(HERE / "lamp_room.jpg", quality=85)


# ---------- data ----------

def thumb(text: str) -> str:
    return f"https://placehold.co/400x400/png?text={text.replace(' ', '+')}"


ITEM = Item(
    category="floor lamp",
    description="mid-century walnut tripod floor lamp with linen drum shade",
    style="mid-century modern",
    colors=["walnut brown", "off-white"],
    materials=["wood", "linen"],
    brand_guess="West Elm",
    confidence=0.72,
    search_queries=[
        "walnut tripod floor lamp linen drum shade",
        "mid-century tripod floor lamp",
        "wood floor lamp",
    ],
    bbox=ROUGH,
    quick_phrase="Mid-century floor lamp. Finding prices.",
)

# (title, store, price, source, tier, source_rank, has_thumbnail, raw CLIP similarity)
# Overstock has no thumbnail (never sent to /embed); the pendant light's thumbnail
# download fails (null similarity) — both must end up unscored after the scored ones.
RAW = [
    ("Mid-Century Tripod Floor Lamp", "West Elm", 349.00, "lens", "new", 0, True, 0.89),
    ("Walnut Tripod Floor Lamp with Linen Shade", "Wayfair", 229.99, "lens", "new", 1, True, 0.86),
    ("Tripod Floor Lamp, Walnut Finish", "Target", 89.99, "lens", "new", 2, True, 0.86),
    ("Wood Tripod Floor Lamp", "Amazon", 119.99, "lens", "new", 3, True, 0.83),
    ("Mid Century Modern Tripod Lamp", "Etsy", 265.00, "lens", "new", 4, True, 0.78),
    ("Scandinavian Tripod Floor Lamp", "IKEA", 59.99, "lens", "new", 5, True, 0.73),
    ("Brass Arc Floor Lamp", "CB2", 299.00, "lens", "new", 6, True, 0.60),
    ("Tripod Floor Lamp with Drum Shade", "Pottery Barn", 399.00, "shopping", "new", 0, True, 0.84),
    ("Walnut Wood Tripod Lamp", "Article", 279.00, "shopping", "new", 1, True, 0.82),
    ("Tripod Floor Lamp", "Walmart", 64.98, "shopping", "new", 2, True, 0.79),
    ("Linen Shade Tripod Floor Lamp", "Crate & Barrel", 329.00, "shopping", "new", 3, True, 0.81),
    ("Modern Floor Lamp, Black Metal", "Home Depot", 49.97, "shopping", "new", 4, True, 0.52),
    ("Mid-Century Floor Lamp", "Overstock", 189.99, "shopping", "new", 5, False, None),
    ("Rattan Pendant Light", "Amazon", 45.99, "shopping", "new", 6, True, None),
    ("West Elm Mid-Century Tripod Floor Lamp - Pre-owned", "eBay", 120.00, "ebay", "used", 0, True, 0.84),
    ("Vintage Walnut Tripod Floor Lamp", "eBay", 95.00, "ebay", "used", 1, True, 0.78),
    ("Tripod Floor Lamp Wood Legs - Used", "eBay", 40.00, "ebay", "used", 2, True, 0.66),
]

LOW, HIGH = 0.55, 0.92  # rerank calibration constants from the spec


def score(sim: float | None) -> int | None:
    if sim is None:
        return None
    return round(min(max((sim - LOW) / (HIGH - LOW), 0.0), 1.0) * 100)


def slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")


def make_products():
    raw, sims = [], []
    for title, store, price, source, tier, rank, has_thumb, sim in RAW:
        raw.append(Product(
            title=title, store=store, price=price, source=source, tier=tier, source_rank=rank,
            url=f"https://example.com/?store={slug(store)}&item={slug(title)}",  # paths 404, root loads
            image_url=thumb(f"{store} {rank}") if has_thumb else None,
        ))
        sims.append(sim)

    # Ranked: same rules rerank.py will use (scored desc, drop < 40, unscored after in source order).
    scored = [p.model_copy(update={"match_score": score(s)}) for p, s in zip(raw, sims)]
    with_score = sorted((p for p in scored if p.match_score is not None), key=lambda p: -p.match_score)
    with_score = [p for p in with_score if p.match_score >= 40]
    without = [p for p in scored if p.match_score is None]
    return raw, with_score + without, sims


def make_search(ranked: list[Product]) -> SearchResponse:
    by_title = {p.title: p for p in ranked}

    # Price insight: non-used with score >= 60 (spec W5). "inclusive" = numpy.percentile's
    # default; it never puts the typical range outside the observed prices.
    comp = sorted(p.price for p in ranked if p.tier != "used" and (p.match_score or 0) >= 60)
    q = statistics.quantiles(comp, n=4, method="inclusive")
    low, high, median = round(q[0], 2), round(q[2], 2), round(statistics.median(comp), 2)

    best = by_title["Mid-Century Tripod Floor Lamp"].model_copy(
        update={"tier": "new", "match_reason": "Same tripod base and drum shade"})

    def pick(title, tier, reason):
        p = by_title[title]
        return p.model_copy(update={
            "tier": tier, "match_reason": reason,
            "savings_pct": round((1 - p.price / best.price) * 100),
        })

    dupes = [
        pick("Tripod Floor Lamp, Walnut Finish", "dupe", "Same silhouette, lighter wood"),
        pick("Wood Tripod Floor Lamp", "dupe", "Similar tripod legs and drum shade"),
    ]
    used = [pick("West Elm Mid-Century Tripod Floor Lamp - Pre-owned", "used", "Likely the same West Elm lamp")]

    verdict = "high" if best.price > high else "low" if best.price < low else "fair"
    savings = round(best.price - dupes[0].price)
    insight = PriceInsight(
        typical_low=low, typical_high=high, median=median, sample_size=len(comp),
        best_match_verdict=verdict,
        verdict_text=(f"West Elm is above the typical ${low:.0f}-${high:.0f} range. "
                      f"The Target dupe saves you ${savings}."),
    )
    return SearchResponse(
        capture_id=CAPTURE_ID, best_match=best, dupes=dupes, used=used, price_insight=insight,
        spoken_summary=(f"That's a West Elm tripod lamp, about {best.price:.0f} dollars. "
                        f"Target has a close lookalike for {dupes[0].price:.0f}."),
        timing_ms={"search": 2100, "rerank": 450, "rank": 1200, "total": 3900}, item=ITEM,
    )


def write(name: str, obj):
    data = obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj
    (HERE / name).write_text(json.dumps(data, indent=2) + "\n")


def main():
    make_images()
    raw, ranked, sims = make_products()
    search = make_search(ranked)

    write("item_lamp.json", ITEM)
    write("products_raw_lamp.json", [p.model_dump(mode="json") for p in raw])
    write("products_ranked_lamp.json", [p.model_dump(mode="json") for p in ranked])
    write("search_response.json", search)
    # Only products with a thumbnail are sent; similarities line up with candidate_urls.
    with_thumb = [(p, s) for p, s in zip(raw, sims) if p.image_url]
    write("embed_service_request.json", {
        "query_url": f"{BASE}/uploads/{CAPTURE_ID}_cutout.jpg",
        "candidate_urls": [p.image_url for p, _ in with_thumb],
    })
    write("embed_service_response.json", {
        "similarities": [s for _, s in with_thumb],
        "latency_ms": 380,
    })
    print(f"wrote fixtures to {HERE}: {len(raw)} raw products, {len(ranked)} ranked, "
          f"best {search.best_match.store} ${search.best_match.price}, "
          f"insight {search.price_insight.typical_low}-{search.price_insight.typical_high} "
          f"({search.price_insight.best_match_verdict})")


if __name__ == "__main__":
    main()
