# Segmentation tester

Streams test videos through the segmentation API the way the glasses would, then writes annotated videos and a report.

## Setup

1. Install ffmpeg (`brew install ffmpeg`) and the Python dependencies:

   ```bash
   pip install -r requirements.txt     # or: uv pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in the Cloudflare Access service token and an Anthropic key (Claude picks each pass's labels):

   ```
   CF_ACCESS_CLIENT_ID=<id>.access
   CF_ACCESS_CLIENT_SECRET=<secret>
   ANTHROPIC_API_KEY=<key>
   ```

## Run

```bash
python run_tests.py                                  # every pass in videos.json
python run_tests.py table bedroom                    # only these videos (all their passes)
python run_tests.py --file my.mp4 --category furniture   # your own video, one pass
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--url` | `https://dubhacks.tamkar.ai/inference` | API base URL |
| `--category` | `things to buy` | Category Claude picks labels in (with `--file`) |
| `--prompt` | picked by Claude | Skip Claude and use these comma-separated phrases |
| `--fps` | 30 | Rate frames are extracted at (the "camera" rate) |
| `--max-frames` | 0 (whole video) | Cap on extracted frames |
| `--no-annotate` | off | Skip writing annotated videos |

## What a pass does

1. Extracts frames from the video with ffmpeg at `--fps`.
2. Has Claude pick labels in `<category>` from the first frame (`backend/labels.py`, the same picker live tracking uses) and uses them as the session prompt. Living things (people, pets, plants) are dropped from the labels and from every frame's results.
3. Opens a session and plays the frames like a live stream: when a response comes back, the frame matching the elapsed time is sent next and the frames in between are dropped.
4. Writes `output/<video>-<category>.mp4` (real-time, showing only the frames that were sent, with boxes and labels) and a summary to `output/report.json`. `report.json` covers only the latest run.

The summary has latency, objects per frame, frames with no objects, and id stability (unique ids, ids that lived under 3 frames, median id lifetime). Many short-lived ids mean tracks are breaking up.

## Videos

`videos.json` lists one entry per pass. A video listed several times with different `category` values runs once per category:

```json
{"name": "table", "path": "table.mp4", "category": "electronics"}
{"name": "bedroom", "url": "https://…/bedroom.mp4", "category": "furniture"}
```

`path` points to a local file in `videos/`. Videos are not committed, so put `table.mp4` there yourself. `url` entries are downloaded to `videos/` on first use. Entries without a `category` use `things to buy`.
