import sys
from pathlib import Path

# Run from backend/ (`pytest tests/`); lets tests import backend modules directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def pin_search_providers(monkeypatch):
    """Tests fake SerpAPI unless they choose OpenWeb Ninja themselves, whatever the local .env says."""
    import search
    monkeypatch.setattr(search, "SHOPPING_PROVIDER", "serpapi")
    monkeypatch.setattr(search, "AMAZON_PROVIDER", "serpapi")
