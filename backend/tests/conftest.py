"""Shared test setup. Tests never read backend/.env or the shell's settings: every setting has its
default unless a test sets it. No network, no GPU, no real keys."""
import logging
import os

import pytest

SETTINGS = (
    "AMAZON_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "BACKEND_URL", "CF_ACCESS_CLIENT_ID",
    "CF_ACCESS_CLIENT_SECRET", "CF_TURN_KEY_API_TOKEN", "CF_TURN_KEY_ID", "EMBED_SERVICE_URL",
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL", "MAX_LABELS", "MODEL_SERVICE_TOKEN",
    "MODEL_SERVICE_URL", "OPENWEB_NINJA_API_KEY", "PREFETCH", "PUBLIC_BASE_URL", "SAM_AUTO_CATEGORY",
    "SAM_CATEGORIES", "SERP_DEV_CACHE", "SERPAPI_KEY", "SHOPPING_PROVIDER", "TURN_CREDENTIAL", "TURN_URLS",
    "TURN_USERNAME", "WARMUP", "WEBRTC_ROTATE",
)

os.environ["STOREFRONT_DOTENV"] = "0"  # config.py skips backend/.env
for name in SETTINGS:
    os.environ.pop(name, None)
logging.getLogger("gtl").setLevel(logging.CRITICAL)  # failure tests log expected warnings


@pytest.fixture(autouse=True)
def pin_search_providers(monkeypatch):
    """Tests fake SerpAPI unless they choose OpenWeb Ninja themselves."""
    import search
    monkeypatch.setattr(search, "SHOPPING_PROVIDER", "serpapi")
    monkeypatch.setattr(search, "AMAZON_PROVIDER", "serpapi")
