"""Records a session's room: every track the backend receives (the phone's camera and mic, the voice agent's
speech), each to its own file in recordings/<room>/ as it comes in, then all of them into room.mp4 once none
is still recording (the phone left, or the room closed). Matroska survives the server being killed mid-way."""
import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from livekit import rtc
from PIL import Image

from storefront import config

log = logging.getLogger("storefront.recorder")

RATE = 48000
MS = Fraction(1, 1000)


class Recorder:
    def __init__(self, room: str):
        self.dir = config.RECORDINGS / room
        self.parts: list[tuple[Path, str, float]] = []  # (file, "video" | "audio", start in s, by monotonic)
        self.active = 0
        self._jobs: dict[str, asyncio.Task] = {}  # track sid -> the task writing it, while it's recorded
        self._mux_lock = asyncio.Lock()

    async def record(self, track: rtc.Track, identity: str, upright: Callable[[rtc.VideoFrameEvent], Image.Image]):
        """Record one track until it ends. Never raises, except when cancelled."""
        kind = "video" if track.kind == rtc.TrackKind.KIND_VIDEO else "audio"
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{len(self.parts):02d}-{identity}-{kind}.mkv"
        self.parts.append((path, kind, time.monotonic()))
        self.active += 1
        log.info("recording %s", path)
        job = self._jobs[track.sid] = asyncio.ensure_future(
            self._video(track, path, upright) if kind == "video" else self._audio(track, path))
        try:
            await job
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():  # the room is closing: close() muxes
                raise
        except Exception as e:
            log.warning("recording %s failed (%r)", path.name, e)
        finally:
            self.active -= 1
            self._jobs.pop(track.sid, None)
        if self.active == 0:
            await self.mux()

    async def _video(self, track, path, upright):
        stream = rtc.VideoStream(track, format=rtc.VideoBufferType.RGBA)
        out = av.open(str(path), "w", format="matroska")
        v, t0, last = None, None, -1
        try:
            async for event in stream:
                img = upright(event)
                now = time.monotonic()
                if v is None:
                    t0 = now
                    v = out.add_stream("libx264", rate=30)
                    v.width, v.height = img.width // 2 * 2, img.height // 2 * 2
                    v.pix_fmt, v.time_base = "yuv420p", MS
                    v.options = {"preset": "veryfast", "crf": "23"}
                if img.size != (v.width, v.height):
                    img = img.resize((v.width, v.height))
                pts = max(int((now - t0) * 1000), last + 1)
                last = pts
                frame = av.VideoFrame.from_image(img)
                frame.pts, frame.time_base = pts, MS
                packets = await asyncio.to_thread(v.encode, frame)
                out.mux(packets)
        finally:
            await stream.aclose()
            if v is not None:
                out.mux(v.encode(None))
            out.close()

    async def _audio(self, track, path):
        stream = rtc.AudioStream(track, sample_rate=RATE, num_channels=1)
        out = av.open(str(path), "w", format="matroska")
        a = out.add_stream("pcm_s16le", rate=RATE, layout="mono")
        n = 0
        try:
            async for event in stream:
                samples = np.frombuffer(event.frame.data, dtype=np.int16).reshape(1, -1)
                frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
                frame.sample_rate, frame.pts, frame.time_base = RATE, n, Fraction(1, RATE)
                n += samples.shape[1]
                out.mux(a.encode(frame))
        finally:
            await stream.aclose()
            out.mux(a.encode(None))
            out.close()

    def stop(self, track: rtc.Track):
        """The track ended (unsubscribed, its participant left): finish its file. Its stream doesn't end by
        itself."""
        job = self._jobs.get(track.sid)
        if job is not None:
            job.cancel()

    async def mux(self):
        """All the parts into room.mp4: the longest video, with every audio track mixed in at its time."""
        async with self._mux_lock:
            videos = [p for p in self.parts if p[1] == "video" and p[0].exists() and p[0].stat().st_size > 1000]
            audios = [p for p in self.parts if p[1] == "audio" and p[0].exists() and p[0].stat().st_size > 1000]
            if not videos and not audios or not shutil.which("ffmpeg"):
                return
            video = max(videos, key=lambda p: p[0].stat().st_size) if videos else None
            t0 = video[2] if video else min(p[2] for p in audios)
            args = ["ffmpeg", "-y", "-loglevel", "error"]
            for path, _, _ in ([video] if video else []) + audios:
                args += ["-i", str(path)]
            first = 1 if video else 0
            chains, labels = [], []
            for i, (_, _, start) in enumerate(audios):
                ms = int((start - t0) * 1000)
                fix = f"adelay={ms}:all=1" if ms > 0 else f"atrim=start={-ms / 1000},asetpts=PTS-STARTPTS"
                chains.append(f"[{first + i}:a]{fix}[a{i}]")
                labels.append(f"[a{i}]")
            if audios:
                chains.append("".join(labels) + f"amix=inputs={len(audios)}:normalize=0[aout]")
                args += ["-filter_complex", ";".join(chains), "-map", "[aout]", "-c:a", "aac"]
            if video:
                args += ["-map", "0:v", "-c:v", "copy"]
            args.append(str(self.dir / "room.mp4"))
            proc = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode:
                log.warning("recording: muxing %s failed: %s", self.dir.name, err.decode()[-300:])
            else:
                log.info("recording: saved %s", self.dir / "room.mp4")
