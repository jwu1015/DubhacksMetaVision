"""Settings from backend/.env (real environment variables win)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
if os.getenv("STOREFRONT_DOTENV") != "0":  # the tests set 0
    load_dotenv(ROOT / ".env")


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
MODEL_SERVICE_URL = os.getenv("MODEL_SERVICE_URL", "").rstrip("/")  # Baptiste's segmentation server
MODEL_SERVICE_TOKEN = os.getenv("MODEL_SERVICE_TOKEN", "")  # bearer token for the CLIP /embed service
# The segmentation server sits behind Cloudflare Access: every request needs this service token.
CF_ACCESS_CLIENT_ID = os.getenv("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.getenv("CF_ACCESS_CLIENT_SECRET", "")
# What SAM tracks (tracker.py), comma-separated; empty = labels Claude picks from the first frame (labels.py)
SAM_CATEGORIES = os.getenv(
    "SAM_CATEGORIES", "sofa,chair,table,lamp,rug,bed,shelf,mirror,wall art,vase,pillow,blanket").strip()
# With SAM_CATEGORIES empty, Claude picks labels within this category instead (2-4.5s more)
SAM_AUTO_CATEGORY = os.getenv("SAM_AUTO_CATEGORY", "furniture and home decor").strip()


def segmentation_headers() -> dict[str, str]:
    """Cloudflare Access headers for the segmentation server; empty if no token is configured."""
    if not CF_ACCESS_CLIENT_ID:
        return {}
    return {"CF-Access-Client-Id": CF_ACCESS_CLIENT_ID, "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET}


# An Anthropic key that isn't scoped to a workspace must name one on every request (vision, match, rank).
ANTHROPIC_WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
ANTHROPIC_HEADERS = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else {}
WARMUP = _flag("WARMUP", "true")  # one vision call at startup so the first real request is fast
# LiveKit Cloud, for the voice assistant (voice.py) and the phone's token for it (POST /voice/token)
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
VOICE_AGENT = "spotted"  # the voice agent's dispatch name: tokens from /voice/token bring it into the room

UPLOADS = ROOT / "uploads"
LOGS = ROOT / "logs"
FIXTURES = ROOT / "fixtures"
