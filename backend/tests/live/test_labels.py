"""Tests for labels.py (Claude's SAM labels), with Claude faked (vlm.VLM.call): no network."""
import base64
import io
import os
import unittest
from unittest import mock

from PIL import Image

import labels
import vlm

PHOTO = Image.new("RGB", (1024, 768), "white")


class LabelsTest(unittest.IsolatedAsyncioTestCase):
    def fake(self, out=None, error=None) -> mock.AsyncMock:
        """Claude answers `out` (labels.LabelsOut) or raises `error`, every time."""
        call = mock.AsyncMock(side_effect=error, return_value=out.model_dump_json() if out else None)
        patcher = mock.patch.object(vlm.haiku, "call", call)
        patcher.start()
        self.addCleanup(patcher.stop)
        return call

    async def test_labels_are_one_general_word_each_without_repeats(self):
        call = self.fake(labels.LabelsOut(labels=["Desk Lamp", "lamp", "cell phone", "cable", ""]))
        self.assertEqual(await labels.generate(PHOTO, "electronics"), ["lamp", "phone", "cable"])
        system, content, schema, max_tokens, timeout = call.await_args.args
        self.assertEqual((system, schema), (labels.SYSTEM, labels.LabelsOut))
        self.assertEqual(content[0]["type"], "image")
        self.assertIn("Category: electronics", content[1]["text"])

    async def test_at_most_max_labels(self):
        self.fake(labels.LabelsOut(labels=[f"thing{i}" for i in range(labels.MAX_LABELS + 5)]))
        self.assertEqual(len(await labels.generate(PHOTO, "stuff")), labels.MAX_LABELS)

    async def test_failures_fall_back_to_the_category(self):
        for why, kw in (("api error", {"error": ValueError("stop_reason=refusal")}),
                        ("no labels", {"out": labels.LabelsOut(labels=[])})):
            with self.subTest(why):
                self.fake(**kw)
                with self.assertRaises(vlm.VLMError):
                    await labels.generate(PHOTO, "Kitchen Utensils")
                self.assertEqual(await labels.labels_or_fallback(PHOTO, "Kitchen Utensils"), ["kitchen utensils"])

    async def test_no_key_falls_back_to_the_category(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(await labels.labels_or_fallback(PHOTO, "furniture"), ["furniture"])

    async def test_the_photo_is_sent_small(self):
        block = vlm.image(PHOTO, labels.EDGE)
        sent = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
        self.assertEqual((sent.format, sent.size), ("JPEG", (512, 384)))


if __name__ == "__main__":
    unittest.main()
