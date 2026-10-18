"""Tests for labels.py (Claude's SAM labels), with the Anthropic client faked: no network.

    python -m unittest test_labels -v      (from backend/)
"""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import labels

PHOTO = Image.new("RGB", (1024, 768), "white")


class FakeMessages:
    def __init__(self, out=None, error=None):
        self.out, self.error, self.calls = out, error, []

    async def parse(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(parsed_output=self.out)


class LabelsTest(unittest.IsolatedAsyncioTestCase):
    def fake(self, out=None, error=None) -> FakeMessages:
        messages = FakeMessages(out, error)
        for p in (mock.patch.object(labels, "client", lambda: SimpleNamespace(messages=messages)),
                  mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"})):
            p.start()
            self.addCleanup(p.stop)
        return messages

    async def test_labels_are_one_general_word_each_without_repeats(self):
        messages = self.fake(labels.LabelsOut(labels=["Desk Lamp", "lamp", "cell phone", "cable", ""]))
        self.assertEqual(await labels.generate(PHOTO, "electronics"), ["lamp", "phone", "cable"])
        call = messages.calls[0]
        self.assertEqual(call["model"], labels.MODEL)
        self.assertEqual(call["output_format"], labels.LabelsOut)
        self.assertIn("Category: electronics", call["messages"][0]["content"][1]["text"])

    async def test_at_most_max_labels(self):
        self.fake(labels.LabelsOut(labels=[f"thing{i}" for i in range(labels.MAX_LABELS + 5)]))
        self.assertEqual(len(await labels.generate(PHOTO, "stuff")), labels.MAX_LABELS)

    async def test_failures_fall_back_to_the_category(self):
        for why, kw in (("api error", {"error": RuntimeError("overloaded")}),
                        ("no labels", {"out": labels.LabelsOut(labels=[])})):
            with self.subTest(why):
                self.fake(**kw)
                with self.assertRaises(labels.LabelError):
                    await labels.generate(PHOTO, "Kitchen Utensils")
                self.assertEqual(await labels.labels_or_fallback(PHOTO, "Kitchen Utensils"), ["kitchen utensils"])

    async def test_no_key_falls_back_without_calling_claude(self):
        messages = self.fake()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(await labels.labels_or_fallback(PHOTO, "furniture"), ["furniture"])
        self.assertEqual(messages.calls, [])


if __name__ == "__main__":
    unittest.main()
