"""Fill the demo cache: send every photo in a folder through /identify once.

    python prewarm.py <folder> [--base http://localhost:8000]

Run it the night before, against the server that will run the demo (the cache lives on that
machine), with DEMO_MODE=false so every answer is live. Each photo costs one Claude call and about
4 SerpAPI searches; SerpAPI's free plan allows ~50 searches an hour, so about 12 photos an hour.
"""
import argparse
import sys
import time
from pathlib import Path

import httpx

TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def photos(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in TYPES)


def short_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        try:
            detail = e.response.json().get("detail")
        except ValueError:
            detail = e.response.text[:100]
        return f"HTTP {e.response.status_code}: {detail}"
    return f"{type(e).__name__}: {e}"


def prewarm(folder: Path, base: str, client: httpx.Client) -> list[tuple[str, str]]:
    """POST each photo to /identify, printing one line per photo. Returns [(file name, problem)]."""
    failures = []
    for path in photos(folder):
        t0 = time.perf_counter()
        try:
            files = {"image": (path.name, path.read_bytes(), TYPES[path.suffix.lower()])}
            body = client.post(f"{base}/identify", files=files).raise_for_status().json()
        except Exception as e:  # report and keep going
            failures.append((path.name, short_error(e)))
            print(f"FAIL  {path.name}: {short_error(e)}")
            continue
        secs = time.perf_counter() - t0
        category = body["detect"]["item"]["category"]
        best = (body.get("search") or {}).get("best_match")
        best_text = f"{best['store']} ${best['price']:,.2f}" if best else "-"
        print(f"{'ok' if best else 'EMPTY':5} {path.name:28} {category or '-':22} {best_text:28} {secs:5.1f}s")
        if not best:  # empty answers aren't saved, so this photo has no fallback in the demo
            failures.append((path.name, "no item found" if not category else "no best match"))
    return failures


def main():
    ap = argparse.ArgumentParser(description="Send every photo in a folder through /identify once.")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--base", default="http://localhost:8000", help="backend URL (default: %(default)s)")
    args = ap.parse_args()
    if not args.folder.is_dir():
        sys.exit(f"not a folder: {args.folder}")
    if not photos(args.folder):
        sys.exit(f"no .jpg/.jpeg/.png photos in {args.folder}")

    t0 = time.perf_counter()
    headers = {"ngrok-skip-browser-warning": "1"}
    with httpx.Client(timeout=120, headers=headers) as client:
        failures = prewarm(args.folder, args.base.rstrip("/"), client)
    n = len(photos(args.folder))
    print(f"\n{n - len(failures)} of {n} photos cached in {time.perf_counter() - t0:.1f}s")
    if failures:
        print("\nNot cached:")
        for name, problem in failures:
            print(f"  {name}: {problem}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
