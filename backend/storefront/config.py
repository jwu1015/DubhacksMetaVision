"""Settings from backend/.env (real environment variables win)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]  # backend/
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
# SAM's prompts are picked from each tracking session's first frame, within a category (labels.py)
DEFAULT_CATEGORY = "small things to buy"
# An object on screen this long without a break gets a quick label, and then a Look (pipeline/runner.py)
QUICK_LABEL_DWELL_MS = int(os.getenv("QUICK_LABEL_DWELL_MS", "500"))
LOOK_DWELL_MS = int(os.getenv("LOOK_DWELL_MS", "500"))
MAX_LOOKS = int(os.getenv("MAX_LOOKS", "3"))  # Looks running at once; each costs a few store searches


def segmentation_headers() -> dict[str, str]:
    """Cloudflare Access headers for the segmentation server; empty if no token is configured."""
    if not CF_ACCESS_CLIENT_ID:
        return {}
    return {"CF-Access-Client-Id": CF_ACCESS_CLIENT_ID, "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET}


# An Anthropic key that isn't scoped to a workspace must name one on every request (vision, match, rank).
ANTHROPIC_WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
ANTHROPIC_HEADERS = {"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else {}
WARMUP = _flag("WARMUP", "true")  # one vision call at startup so the first real request is fast
# LiveKit Cloud: the session's room (rtc/room.py, POST /session/token) and the voice agent (voice/agent.py)
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
VOICE_AGENT = "storefront"  # the voice agent's dispatch name: tokens from /voice/token bring it into the room
# The voice agent's model: Gemini Live, through LiveKit's Google plugin (it reads GOOGLE_API_KEY)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-live")


def livekit_ready() -> bool:
    return bool(LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)


STATIC = ROOT / "static"
UPLOADS = ROOT / "uploads"
RECORDINGS = ROOT / "recordings"  # each session room, recorded (rtc/recorder.py)
RECORD_ROOMS = _flag("RECORD_ROOMS", "true")
LOGS = ROOT / "logs"
FIXTURES = ROOT / "fixtures"
