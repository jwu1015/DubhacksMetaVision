"""W6: demo safety net. Saves /detect and /search answers per photo and serves them when live calls fail.

    cache/<sha>/<kind>.json      sha = SHA-256 of the uploaded photo's bytes

Good live answers are always saved. DEMO_MODE=true: if the live call errors, takes over 8s, or comes
back empty (no item, no best match), serve the saved answer instead. DEMO_MODE=false: live only.
Fill the cache the night before with prewarm.py.
"""
import asyncio
import hashlib
import json
import logging
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, ValidationError

import config

log = logging.getLogger("gtl.cache")

CACHE_DIR = config.ROOT / "cache"
DEMO_TIMEOUT_S = 8.0
BASE = "__BASE__"  # stands in for PUBLIC_BASE_URL on disk, so a new ngrok URL doesn't break saved links

Kind = Literal["detect", "search"]


def image_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get(sha: str, kind: Kind) -> dict | None:
    """The saved answer with the current PUBLIC_BASE_URL swapped back in, or None."""
    try:
        text = (CACHE_DIR / sha / f"{kind}.json").read_text()
        return json.loads(text.replace(BASE, config.PUBLIC_BASE_URL))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:  # unreadable or half-written file: treat as missing
        log.warning("cache: can't read %s/%s.json (%s)", sha[:12], kind, e)
        return None


def put(sha: str, kind: Kind, response: BaseModel) -> None:
    text = response.model_dump_json(indent=2)
    if config.PUBLIC_BASE_URL:
        text = text.replace(config.PUBLIC_BASE_URL, BASE)
    path = CACHE_DIR / sha / f"{kind}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)  # atomic, so a crash mid-write never leaves half a file


def usable(kind: Kind, response) -> bool:
    """False for empty answers, which happen when something upstream failed quietly.

    Search never raises (a dead network just gives no products), so emptiness is the failure signal.
    """
    if kind == "detect":
        return response.item.category is not None
    return response.best_match is not None


def _saved(sha: str, kind: Kind, model: type[BaseModel]) -> BaseModel | None:
    data = get(sha, kind)
    if data is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError:  # saved before models.py changed
        log.warning("cache: %s/%s.json no longer matches %s; ignoring it", sha[:12], kind, model.__name__)
        return None


async def demo_guard(sha: str, kind: Kind, make: Callable[[], Awaitable[BaseModel]],
                     model: type[BaseModel]) -> BaseModel:
    """Run the live call; in DEMO_MODE, fall back to the saved answer if it fails, hangs or comes back empty."""
    saved = _saved(sha, kind, model) if config.DEMO_MODE else None
    try:
        if saved is not None:
            live = await asyncio.wait_for(make(), DEMO_TIMEOUT_S)
        else:
            live = await make()  # nothing to fall back on: a slow answer beats an error
    except Exception as e:
        if saved is None:
            raise
        log.warning("cache: %s %s live call failed (%s); serving the saved answer", kind, sha[:12],
                    type(e).__name__)
        return saved
    if usable(kind, live):
        put(sha, kind, live)
        return live
    if saved is not None:
        log.warning("cache: %s %s live answer was empty; serving the saved answer", kind, sha[:12])
        return saved
    return live
