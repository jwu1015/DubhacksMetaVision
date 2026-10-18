"""Claude and the stores, faked for a Look: no network, no API keys."""
import asyncio

from storefront.models import Item, PriceInsight, SearchResponse
from storefront.pipeline import rank, rerank, search, vision

CLAUDE_ITEM = Item(category="insulated water bottle", description="Green Owala FreeSip, lime spout cover.",
                   search_queries=["Owala FreeSip 24 oz green"])


class Stores:
    """Records what Claude and the stores were given. Clear `gate` to hold the stores' answer; set `fail` to
    make the next search fail, and `item` to change Claude's answer (an exception raises it)."""

    def __init__(self, monkeypatch):
        self.described: list[bytes] = []
        self.contexts: list[tuple] = []  # (category, label) Claude was told
        self.searched: list[tuple] = []  # (cutout, queries, look_id)
        self.gate = asyncio.Event()
        self.gate.set()
        self.fail = False
        self.item: Item | Exception = CLAUDE_ITEM
        for target, name, value in ((vision, "detect", self.detect), (search, "search_all", self.search_all),
                                    (rerank, "rerank", self.rerank), (rank, "choose", self.choose)):
            monkeypatch.setattr(target, name, value)

    async def detect(self, data, hint=None, category=None, label=None):
        self.described.append(data)
        self.contexts.append((category, label))
        if isinstance(self.item, Exception):
            raise self.item
        return self.item

    async def search_all(self, cutout, item, capture_id=None):
        self.searched.append((cutout, item.search_queries, capture_id))
        await self.gate.wait()
        if self.fail:
            self.fail = False
            raise RuntimeError("stores down")
        return []

    async def rerank(self, cutout, products):
        return products

    async def choose(self, item, products, insight: PriceInsight):
        return SearchResponse(capture_id="", best_match=None, dupes=[], used=[], price_insight=insight,
                              spoken_summary="I couldn't find that one for sale.", timing_ms={})
