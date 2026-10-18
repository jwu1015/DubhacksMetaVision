"""Claude Haiku for the backend's quick structured calls, all through one VLM:
- labels.py: what SAM tracks, from a stream's first frame
- prefetch.py: a quick name and price range for a tracked object's crop
- rank.py: the best match, dupes and used options among the search results

ask() sends a system prompt and a user message (text, or a list of content blocks; image() makes one
from a photo), and returns the reply parsed into a pydantic model. Thinking is off and effort low:
these are extraction calls, and the output length is what costs time. ask() never returns bad
output: an API error, a refusal or a reply that doesn't fit the model raises VLMError, after
`attempts` tries within `timeout` seconds in all.

vision.py and match.py call Sonnet with their own settings and don't go through here.
"""
import base64
import io
import logging
import os
import time
from typing import TypeVar

import anthropic
from PIL import Image, ImageOps
from pydantic import BaseModel, ValidationError

from storefront import (
    config,  # loads .env before the Anthropic client reads ANTHROPIC_API_KEY
)

log = logging.getLogger("storefront.vlm")

MODEL = "claude-haiku-5-5"
MIN_ATTEMPT_S = 1.0  # a retry with less time left than this would only time out

Out = TypeVar("Out", bound=BaseModel)


class VLMError(Exception):
    pass


def image(src: Image.Image | bytes, edge: int | None = None) -> dict:
    """A JPEG content block. A PIL image is shrunk to at most `edge` px on its long side; JPEG bytes go
    as they are, unless `edge` is given."""
    if isinstance(src, bytes) and edge is None:
        data = src
    else:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(src))) if isinstance(src, bytes) else src.copy()
        if edge:
            img.thumbnail((edge, edge))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        data = buf.getvalue()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(data).decode()}}


def ms_since(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


class VLM:
    def __init__(self, model: str = MODEL):
        self.model = model
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            # No SDK retries: ask() retries itself, within its time budget.
            self._client = anthropic.AsyncAnthropic(max_retries=0, default_headers=config.ANTHROPIC_HEADERS)
        return self._client

    async def call(self, system: str, content: str | list[dict], schema: type[BaseModel], max_tokens: int,
                   timeout: float) -> str:
        """One request; the reply's text. Raises on API errors and when the reply didn't finish."""
        if not os.getenv("ANTHROPIC_API_KEY"):  # else the SDK raises a TypeError on the request
            raise anthropic.AnthropicError("ANTHROPIC_API_KEY not set")
        msg = await self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, timeout=timeout,
            messages=[{"role": "user", "content": content}],
            thinking={"type": "disabled"},  # adaptive at low effort still thought: 3.7s+ vs ~2.2s (rank)
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}},
        )
        if msg.stop_reason != "end_turn":
            raise ValueError(f"stop_reason={msg.stop_reason}")
        return "".join(b.text for b in msg.content if b.type == "text")

    async def ask(self, name: str, system: str, content: str | list[dict], out: type[Out], *,
                  max_tokens: int, timeout: float, attempts: int = 1) -> Out:
        """The reply as `out`. `name` is for the logs. Raises VLMError after `attempts` failed tries, or
        once less than MIN_ATTEMPT_S of `timeout` is left for another."""
        deadline = time.perf_counter() + timeout
        error = "no attempt"
        for attempt in range(1, attempts + 1):
            left = deadline - time.perf_counter()
            if attempt > 1 and left < MIN_ATTEMPT_S:
                break
            t0 = time.perf_counter()
            try:
                reply = out.model_validate_json(await self.call(system, content, out, max_tokens, left))
            except (anthropic.AnthropicError, ValueError, ValidationError) as e:  # ValueError: bad JSON, refusal
                error = f"{type(e).__name__}: {' '.join(str(e).split())[:200]}"
                log.warning("%s: claude attempt %d failed after %dms: %s", name, attempt, ms_since(t0), error)
                continue
            log.info("%s: claude %dms (attempt %d)", name, ms_since(t0), attempt)
            return reply
        raise VLMError(error)

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None


haiku = VLM()
