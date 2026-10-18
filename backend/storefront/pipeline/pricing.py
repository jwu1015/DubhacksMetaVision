"""W5: price insight, the typical price range of comparable new products. Pure, no I/O.

The verdict needs the best match, which isn't known yet; rank.choose fills it in.
"""
import statistics

from storefront.models import PriceInsight, Product

MIN_SCORE = 60   # "comparable" means a close visual match...
MIN_SAMPLE = 4   # ...unless fewer than this many qualify; then every non-used product counts


def comparison_set(products: list[Product]) -> list[Product]:
    new = [p for p in products if p.tier != "used"]
    close = [p for p in new if p.match_score is not None and p.match_score >= MIN_SCORE]
    return close if len(close) >= MIN_SAMPLE else new


def money(x: float | None) -> float | None:
    return None if x is None else round(x, 2)


def compute_price_insight(products: list[Product]) -> PriceInsight:
    """25th-75th percentile range and median of the comparison set's prices."""
    prices = sorted(p.price for p in comparison_set(products))
    if not prices:
        low = high = median = None
    elif len(prices) == 1:
        low = high = median = prices[0]
    else:
        # "inclusive" never puts the range outside the observed prices (team decision).
        q = statistics.quantiles(prices, n=4, method="inclusive")
        low, high, median = q[0], q[2], statistics.median(prices)
    return PriceInsight(
        typical_low=money(low), typical_high=money(high), median=money(median),
        sample_size=len(prices), best_match_verdict="fair", verdict_text="",
    )
