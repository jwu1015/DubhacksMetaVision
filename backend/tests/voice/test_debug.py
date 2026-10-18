"""Tests for the voice agent's debug logs (voice/debug.py): the frames sent to Gemini are counted and summed up
in one line, and still reach Gemini. No LiveKit, no Gemini."""
import importlib
import unittest
from unittest import mock

import numpy as np
from livekit import rtc

from storefront.voice import debug
from storefront.voice.debug import Flow
from storefront.voice.seen import Seen

MUG = {"uid": "1.1", "prompt": "mug", "score": 0.9, "box": [0.4, 0.4, 0.6, 0.6], "in_view": True, "last_seen_ts": 0}


def mic_frame(level=1000, rate=48000) -> rtc.AudioFrame:
    n = rate // 100
    return rtc.AudioFrame(np.full(n, level, dtype=np.int16).tobytes(), rate, 1, n)


def camera_frame() -> rtc.VideoFrame:
    return rtc.VideoFrame(4, 2, rtc.VideoBufferType.RGBA, bytes(4 * 2 * 4))


class FakeSession:
    def __init__(self):
        self.audio, self.video = [], []

    def push_audio(self, frame):
        self.audio.append(frame)

    def push_video(self, frame):
        self.video.append(frame)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.seen = Seen()
        self.clock = mock.patch.object(debug.time, "monotonic", return_value=100.0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.flow = Flow(self.seen)

    def test_the_agent_module_loads(self):
        importlib.import_module("storefront.voice.agent")

    def test_frames_still_reach_gemini(self):
        rt = FakeSession()
        self.flow.watch(rt)
        rt.push_audio(mic_frame())
        rt.push_video(camera_frame())
        self.assertEqual((len(rt.audio), len(rt.video)), (1, 1))

    def test_one_line_every_few_seconds(self):
        self.seen.update({"type": "objects", "objects": [MUG]})
        with self.assertLogs("storefront.voice", "INFO") as logs:
            for _ in range(100):  # 1 s of mic
                self.flow.audio(mic_frame())
            self.flow.video(camera_frame())
            self.now.return_value = 100.0 + debug.EVERY_S
            self.flow.audio(mic_frame())
        summary = [line for line in logs.output if "to Gemini:" in line]
        self.assertEqual(len(summary), 1)
        self.assertIn("mic 1.0s (loudest -30 dBFS), camera 1 frames 4x2", summary[0])
        self.assertIn("tracker messages {'objects': 1}, 1 objects, camera on 1.1 (mug)", summary[0])
        self.assertFalse(self.seen.received)  # counted afresh for the next line

    def test_silence_and_first_frames(self):
        with self.assertLogs("storefront.voice", "INFO") as logs:
            self.flow.audio(mic_frame(level=0))
            self.flow.video(camera_frame())
            self.now.return_value = 100.0 + debug.EVERY_S
            self.flow.audio(mic_frame(level=0))
        self.assertTrue(any("first mic frame to Gemini (48000 Hz, 1 ch)" in line for line in logs.output))
        self.assertTrue(any("first camera frame to Gemini (4x2)" in line for line in logs.output))
        self.assertTrue(any("(loudest silent)" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
