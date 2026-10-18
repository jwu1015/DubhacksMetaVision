"""Tests for quick_label.py, with Claude faked (vlm.VLM.call): no network."""
import asyncio
import json

import anthropic
import httpx
import pytest

from storefront.pipeline import quick_label, vlm


def answer(monkeypatch, reply):
    async def call_model(system, content, schema, max_tokens, timeout):
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)
    monkeypatch.setattr(vlm.haiku, "call", call_model)


@pytest.mark.parametrize("reply, expected", [
    ({"name": "black insulated water bottle", "price_low": 30, "price_high": 50},
     ("black insulated water bottle", 30, 50)),
    ({"name": "  Fuzzy   slippers ", "price_low": 60, "price_high": 25}, ("Fuzzy slippers", 25, 60)),
    ({"name": "slippers", "price_low": 0, "price_high": 30}, None),
    ({"name": " ", "price_low": 10, "price_high": 30}, None),
])
def test_the_answer_is_cleaned_up(monkeypatch, reply, expected):
    answer(monkeypatch, reply)
    q = asyncio.run(quick_label.quick_label(b"jpeg"))
    assert ((q.name, q.price_low, q.price_high) if q else None) == expected


def test_a_failure_gives_none(monkeypatch):
    answer(monkeypatch, anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")))
    assert asyncio.run(quick_label.quick_label(b"jpeg")) is None


def test_claude_is_told_the_category_and_the_label(monkeypatch):
    asked = []

    async def call_model(system, content, schema, max_tokens, timeout):
        asked.append(content[-1]["text"])
        return json.dumps({"name": "black phone", "price_low": 300, "price_high": 900})
    monkeypatch.setattr(vlm.haiku, "call", call_model)
    asyncio.run(quick_label.quick_label(b"jpeg", "electronics", "phone"))
    asyncio.run(quick_label.quick_label(b"jpeg"))
    assert asked == ['The user is looking for: electronics\nThe tracker found this as: "phone"\nLabel this item.',
                     "Label this item."]
