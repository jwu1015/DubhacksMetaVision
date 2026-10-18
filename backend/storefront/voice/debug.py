"""Debug logs for the voice agent: what reaches Gemini (the phone's mic and camera frames, the tracker's messages),
what it hears and says, and its tool calls. Frames are summed up every EVERY_S seconds, not logged one by one."""
import logging
import math
import time

import numpy as np
from livekit import rtc
from livekit.agents import AgentSession, llm
from livekit.plugins import google

from storefront.voice.seen import Seen

log = logging.getLogger("storefront.voice")

EVERY_S = 5.0
CLIP = 300  # characters of a tool's output in the log


def clip(text: str | None) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= CLIP else text[:CLIP] + "..."


class Flow:
    """Counts the frames sent to Gemini, and logs a line about them (and the tracker's messages) every EVERY_S
    seconds while they come. No line at all means no frames: the phone's mic isn't published or subscribed."""

    def __init__(self, seen: Seen):
        self.seen = seen
        self.since = time.monotonic()
        self.first_audio = self.first_video = True
        self._reset()

    def _reset(self):
        self.audio_s, self.loudest, self.frames, self.size = 0.0, 0.0, 0, None

    def audio(self, f: rtc.AudioFrame):
        if self.first_audio:
            self.first_audio = False
            log.info("voice: first mic frame to Gemini (%d Hz, %d ch)", f.sample_rate, f.num_channels)
        self.audio_s += f.samples_per_channel / f.sample_rate
        samples = np.frombuffer(f.data, dtype=np.int16)
        if samples.size:
            self.loudest = max(self.loudest, float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768)
        self._tick()

    def video(self, f: rtc.VideoFrame):
        if self.first_video:
            self.first_video = False
            log.info("voice: first camera frame to Gemini (%dx%d)", f.width, f.height)
        self.frames += 1
        self.size = f"{f.width}x{f.height}"
        self._tick()

    def _tick(self):
        now = time.monotonic()
        if now - self.since < EVERY_S:
            return
        db = f"{20 * math.log10(self.loudest):.0f} dBFS" if self.loudest > 0 else "silent"
        current = self.seen.current()
        log.info("voice: last %.0fs to Gemini: mic %.1fs (loudest %s), camera %d frames %s; tracker messages %s, "
                 "%d objects, camera on %s", now - self.since, self.audio_s, db, self.frames, self.size or "",
                 dict(self.seen.received) or "none", len(self.seen.objects),
                 f"{current['uid']} ({current['prompt']})" if current else "nothing")
        self.seen.received.clear()
        self.since = now
        self._reset()

    def watch(self, rt: llm.RealtimeSession):
        """Count what the agent pushes to this Gemini Live session."""
        push_audio, push_video = rt.push_audio, rt.push_video

        def audio(frame: rtc.AudioFrame):
            self.audio(frame)
            push_audio(frame)

        def video(frame: rtc.VideoFrame):
            self.video(frame)
            push_video(frame)

        rt.push_audio, rt.push_video = audio, video


class RealtimeModel(google.realtime.RealtimeModel):
    """Gemini Live, with every frame it's sent counted by flow."""

    def __init__(self, flow: Flow, **kwargs):
        super().__init__(**kwargs)
        self._flow = flow

    def session(self, **kwargs) -> llm.RealtimeSession:
        rt = super().session(**kwargs)
        self._flow.watch(rt)
        return rt


def log_session(session: AgentSession):
    """Log what the user said, what the agent said, its tool calls and state, and errors."""
    @session.on("user_input_transcribed")
    def _(e):
        if e.is_final:
            log.info("voice: heard %r", e.transcript)

    @session.on("user_state_changed")
    def _(e):
        log.info("voice: user %s", e.new_state)

    @session.on("agent_state_changed")
    def _(e):
        log.info("voice: agent %s", e.new_state)

    @session.on("function_tools_executed")
    def _(e):
        for call, out in e.zipped():
            log.info("voice: tool %s(%s) -> %s%s", call.name, call.arguments, "error: " if out.is_error else "",
                     clip(out.output))

    @session.on("conversation_item_added")
    def _(e):
        if getattr(e.item, "role", None) == "assistant":
            log.info("voice: said %r", e.item.text_content)

    @session.on("error")
    def _(e):
        log.warning("voice: error from %s: %r", type(e.source).__name__, e.error)

    @session.on("close")
    def _(e):
        log.info("voice: session closed (%s)", e.reason)
