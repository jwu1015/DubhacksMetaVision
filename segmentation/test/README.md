# Segmentation tester

Streams test videos through the segmentation API the way the glasses would, then writes annotated videos and a report.

## Setup

1. Install ffmpeg (`brew install ffmpeg`) and the Python dependencies:

   ```bash
   pip install -r requirements.txt     # or: uv pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in the Cloudflare Access service token:

   ```
   CF_ACCESS_CLIENT_ID=<id>.access
   CF_ACCESS_CLIENT_SECRET=<secret>
   ```

## Run

```bash
python run_tests.py                                  # every pass in videos.json
python run_tests.py table bedroom                    # only these videos (all their passes)
python run_tests.py --file my.mov --category furniture   # your own video, one pass
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--url` | `https://dubhacks.tamkar.ai/inference` | API base URL |
| `--category` | `things to buy` | Category sent to `/labels` (with `--file`) |
| `--prompt` | from `/labels` | Skip `/labels` and use these comma-separated phrases |
| `--fps` | 30 | Rate frames are extracted at (the "camera" rate) |
| `--max-frames` | 0 (whole video) | Cap on extracted frames |
| `--no-annotate` | off | Skip writing annotated videos |

## What a pass does

1. Extracts frames from the video with ffmpeg at `--fps`.
2. Sends the first frame (raw JPEG) to `POST /labels?category=<category>` and uses the returned labels as the session prompt. Living things (people, pets, plants) are dropped from the labels and from every frame's results.
3. Opens a session and plays the frames like a live stream: when a response comes back, the frame matching the elapsed time is sent next and the frames in between are dropped.
4. Writes `output/<video>-<category>.mp4` (real-time, showing only the frames that were sent, with boxes and labels) and a summary to `output/report.json`. `report.json` covers only the latest run.

The summary has latency, objects per frame, frames with no objects, and id stability (unique ids, ids that lived under 3 frames, median id lifetime). Many short-lived ids mean tracks are breaking up.

## Videos

`videos.json` lists one entry per pass. A video listed several times with different `category` values runs once per category:

```json
{"name": "table", "path": "table.mov", "category": "electronics"}
{"name": "bedroom", "url": "https://…/bedroom.mp4", "category": "furniture"}
```

`path` points to a file in `videos/` (`table.mov` is checked in). `url` entries are downloaded to `videos/` on first use. Entries without a `category` use `things to buy`, since the server requires one.
