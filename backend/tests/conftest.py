import sys
from pathlib import Path

# Run from backend/ (`pytest tests/`); lets tests import backend modules directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
