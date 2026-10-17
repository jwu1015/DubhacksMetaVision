"""The one shared httpx.AsyncClient. Always pass a per-call timeout."""
import httpx

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    """Created on first use, so CLI scripts can use it without the app running."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
    return _client


async def close():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
