"""Settings from backend/.env (real environment variables win)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
MODEL_SERVICE_URL = os.getenv("MODEL_SERVICE_URL", "").rstrip("/")
MODEL_SERVICE_TOKEN = os.getenv("MODEL_SERVICE_TOKEN", "")
USE_FIXTURES = _flag("USE_FIXTURES")
DEMO_MODE = _flag("DEMO_MODE")
WARMUP = _flag("WARMUP", "true")  # one vision call at startup so the first real request is fast

UPLOADS = ROOT / "uploads"
LOGS = ROOT / "logs"
FIXTURES = ROOT / "fixtures"
