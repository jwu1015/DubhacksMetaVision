"""Tester for the segmentation server: streams test videos through a tracking session.

  python run_tests.py                      # all videos in videos.json (downloaded on first use)
  python run_tests.py bedroom table        # only these
  python run_tests.py --file my.mp4 --category furniture   # your own video
Options: --url https://dubhacks.tamkar.ai/inference  --fps 30  --max-frames 0  --no-annotate

For each video: extracts frames with ffmpeg at --fps (the "camera" rate), sends the first frame (raw
JPEG) to POST /labels?category=<category> and uses the returned labels as the session prompt
(--prompt overrides this). A video listed once per category in videos.json runs once per category.
It then plays the frames against a new session like a live stream: each time a response comes back,
the frame matching the elapsed wall-clock time is sent next and the frames in between are dropped.
Reports latency, objects per frame and id stability. Annotated videos (real-time, showing only the
frames that were sent) and a JSON report go to output/. Exit code is 1 if any video errored.
Setup (pip install -r requirements.txt, ffmpeg, .env with the service token): see README.md.
"""
import argparse
import colorsys
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
VIDEOS = HERE / "videos"
OUTPUT = HERE / "output"
DEFAULT_CATEGORY = "things to buy"  # the server requires a category for /labels
load_dotenv(HERE / ".env")
AUTH = {
    "CF-Access-Client-Id": os.environ.get("CF_ACCESS_CLIENT_ID", ""),
    "CF-Access-Client-Secret": os.environ.get("CF_ACCESS_CLIENT_SECRET", ""),
}


def download(entry):
    if "path" in entry:  # local file already in videos/
        return VIDEOS / entry["path"]
    path = VIDEOS / f"{entry['name']}.mp4"
    if not path.exists():
        VIDEOS.mkdir(exist_ok=True)
        print(f"  downloading {entry['url']}")
        r = requests.get(entry["url"], timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def extract_frames(video, out_dir, fps, max_frames):
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={fps}", "-q:v", "3"]
    if max_frames:
        cmd += ["-frames:v", str(max_frames)]
    subprocess.run(cmd + [str(out_dir / "%05d.jpg")], check=True)
    return sorted(out_dir.glob("*.jpg"))


# The pipeline never boxes living things, whatever the server returns or the prompt asks for.
LIVING = {"person", "people", "man", "woman", "child", "kid", "baby", "face", "hand", "dog", "cat", "pet",
          "animal", "bird", "horse", "plant", "flower", "tree"}


def is_living(obj):
    return (obj.get("prompt") or "").strip().lower() in LIVING


def generate_prompt(url, frame_path, category):
    """POST the first frame to /labels; returns the labels (minus living things) to use as the session prompt."""
    r = requests.post(f"{url}/labels", params={"category": category}, data=frame_path.read_bytes(),
                      headers={**AUTH, "Content-Type": "image/jpeg"}, timeout=120)
    r.raise_for_status()
    return [l for l in r.json()["labels"] if l.strip().lower() not in LIVING]


def annotate(frame_path, objects):
    img = Image.open(frame_path).convert("RGB")
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=max(14, img.height // 40))
    for o in objects:
        hue = (o["id"] * 0.61803) % 1
        rgb = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.85, 1.0))
        d.rectangle(o["box"], outline=rgb, width=3)
        label = f"#{o['id']} {o.get('prompt') or ''} {o['score']:.2f}"
        x, y = o["box"][0], o["box"][1]
        l, t, r, b = d.textbbox((x, y), label, font=font)
        top = max(0, t - 4)  # keep the label inside the frame for boxes touching the top edge
        d.rectangle((l, top, r + 4, top + (b - t) + 4), fill=rgb)
        d.text((l + 2, top + 2), label, fill=(0, 0, 0), font=font)
    return img


def run_video(name, video, prompt, category, args):
    print(f"[{name}] category={category!r} prompt={prompt!r}")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        frames_dir = tmp / "frames"
        frames_dir.mkdir()
        frames = extract_frames(video, frames_dir, args.fps, args.max_frames)
        if not frames:
            raise RuntimeError("ffmpeg produced no frames")

        if not prompt:
            prompt = generate_prompt(args.url, frames[0], category)
            print(f"  /labels -> {prompt}")
            if not prompt:
                raise RuntimeError("/labels returned no usable labels")
        sid = requests.post(f"{args.url}/session", json={"prompt": prompt}, headers=AUTH, timeout=60)
        sid.raise_for_status()
        sid = sid.json()["session_id"]
        latencies, per_frame, sent = [], [], []  # sent: index into frames of each frame we sent
        try:
            start = time.perf_counter()
            idx = 0
            while True:
                t = time.perf_counter()
                r = requests.post(f"{args.url}/session/{sid}/frame", data=frames[idx].read_bytes(),
                                  headers={**AUTH, "Content-Type": "image/jpeg"}, timeout=120)
                r.raise_for_status()
                latencies.append(time.perf_counter() - t)
                per_frame.append([o for o in r.json()["objects"] if not is_living(o)])
                sent.append(idx)
                if len(sent) % 10 == 1:
                    print(f"  sent {len(sent)} (video frame {idx + 1}/{len(frames)}): "
                          f"{len(per_frame[-1])} objects, {latencies[-1] * 1000:.0f} ms")
                if idx == len(frames) - 1:
                    break
                # the live stream moved on while we waited: jump to the newest frame, never go backwards
                idx = min(max(int((time.perf_counter() - start) * args.fps), idx + 1), len(frames) - 1)
        finally:
            requests.delete(f"{args.url}/session/{sid}", headers=AUTH, timeout=30)

        if not args.no_annotate:
            ann_dir = tmp / "ann"
            ann_dir.mkdir()
            # each sent frame is shown until the next sent frame's video time, so playback is real-time
            concat = []
            for i, (fi, objs) in enumerate(zip(sent, per_frame)):
                out = ann_dir / f"{i:05d}.jpg"
                annotate(frames[fi], objs).save(out, quality=90)
                nxt = sent[i + 1] if i + 1 < len(sent) else fi + 1
                concat += [f"file '{out}'", f"duration {(nxt - fi) / args.fps:.4f}"]
            concat.append(f"file '{ann_dir / f'{len(sent) - 1:05d}.jpg'}'")  # concat demuxer drops the last duration
            (tmp / "concat.txt").write_text("\n".join(concat))
            OUTPUT.mkdir(exist_ok=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp / "concat.txt"),
                            "-vsync", "vfr", "-pix_fmt", "yuv420p", str(OUTPUT / f"{name}.mp4")], check=True)

    # id stability: how long each id lives. Many short-lived ids => track fragmentation / flicker.
    spans = {}
    for i, objs in enumerate(per_frame):
        for o in objs:
            spans.setdefault(o["id"], []).append(i)
    lifetimes = [len(v) for v in spans.values()]
    counts = [len(o) for o in per_frame]
    summary = {
        "name": name, "category": category, "prompt": prompt, "frames": len(sent), "video_frames": len(frames),
        "latency_ms_mean": round(statistics.mean(latencies) * 1000, 1),
        "latency_ms_p95": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))] * 1000, 1),
        "throughput_fps": round(len(sent) / sum(latencies), 2),
        "objects_per_frame_mean": round(statistics.mean(counts), 2),
        "frames_with_no_objects": sum(1 for c in counts if c == 0),
        "unique_ids": len(spans),
        "short_lived_ids(<3 frames)": sum(1 for n in lifetimes if n < 3),
        "median_id_lifetime_frames": statistics.median(lifetimes) if lifetimes else 0,
    }
    return summary, per_frame


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="video names from videos.json (default: all)")
    ap.add_argument("--file", help="run on your own video file instead")
    ap.add_argument("--category", help="category sent to /labels with --file")
    ap.add_argument("--prompt", help="override the prompt (comma-separated phrases); default is generated by /labels from the first frame")
    ap.add_argument("--url", default="https://dubhacks.tamkar.ai/inference")
    ap.add_argument("--fps", type=float, default=30, help="camera rate frames are extracted at")
    ap.add_argument("--max-frames", type=int, default=0, help="cap on extracted frames (0 = whole video)")
    ap.add_argument("--no-annotate", action="store_true")
    args = ap.parse_args()

    try:
        r = requests.get(f"{args.url}/health", headers=AUTH, timeout=10, allow_redirects=False)
        if r.status_code in (302, 401, 403):
            sys.exit(f"Cloudflare Access rejected the request ({r.status_code}); check CF_ACCESS_CLIENT_ID/SECRET in .env")
        health = r.json()
    except requests.RequestException as e:
        sys.exit(f"server not reachable at {args.url}: {e}")
    print(f"server: {health}\n")

    jobs = []
    if args.file:
        p = Path(args.file)
        jobs.append((p.stem, p, args.category or DEFAULT_CATEGORY, args.prompt))
    else:
        catalog = json.loads((HERE / "videos.json").read_text())
        unknown = set(args.names) - {e["name"] for e in catalog}
        if unknown:
            sys.exit(f"unknown video(s): {', '.join(sorted(unknown))}")
        for e in catalog:
            if not args.names or e["name"] in args.names:
                jobs.append((e["name"], download(e), e.get("category", DEFAULT_CATEGORY), args.prompt))

    results, failed = [], 0
    for name, path, category, prompt in jobs:
        name = f"{name}-{category.replace(' ', '-')}"  # one output per (video, category) pass
        try:
            summary, per_frame = run_video(name, path, prompt, category, args)
            results.append({"summary": summary, "frames": per_frame})
        except Exception as e:  # keep going so one bad video doesn't hide the rest
            failed += 1
            print(f"[{name}] FAILED: {e}")

    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / "report.json").write_text(json.dumps(results, indent=1))
    print("\n=== summary ===")
    for r in results:
        print(json.dumps(r["summary"]))
    print(f"\n{len(results)} ok, {failed} failed. Annotated videos + report.json in {OUTPUT}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
