"""Tests for scene.py: what scene memory keeps about each tracked object, and its best frame."""
import time
import unittest

import numpy as np
from PIL import Image, ImageFilter

from storefront.live import scene
from storefront.live.scene import Scene, centeredness, sharpness
from storefront.live.tracker import TrackedObject, TrackResult
from storefront.models import BBox

CENTER = BBox(x=0.4, y=0.4, w=0.2, h=0.2)
LEFT = BBox(x=0.0, y=0.4, w=0.2, h=0.2)
T0 = time.time()  # best() forgets tracks against the real clock


def checkerboard(size=(320, 240), cell=8) -> Image.Image:
    y, x = np.indices((size[1], size[0]))
    return Image.fromarray(((x // cell + y // cell) % 2 * 255).astype(np.uint8)).convert("RGB")


SHARP = checkerboard()
BLURRY = SHARP.filter(ImageFilter.GaussianBlur(4))


def frame(t: float, *objects: tuple[int, str, BBox], idx: int = 0) -> TrackResult:
    return TrackResult(frame_idx=idx, width=320, height=240, latency_ms=200, captured_at=t,
                       objects=[TrackedObject(id=i, label=label, score=0.9, box=box) for i, label, box in objects])


class SceneTest(unittest.TestCase):
    def test_centeredness_and_sharpness(self):
        self.assertEqual(centeredness(CENTER), 1.0)
        self.assertEqual(centeredness(BBox(x=0.0, y=0.0, w=0.0, h=0.0)), 0.0)  # a corner
        self.assertGreater(centeredness(CENTER), centeredness(LEFT))
        self.assertGreater(sharpness(SHARP, CENTER), 10 * sharpness(BLURRY, CENTER))
        self.assertEqual(sharpness(SHARP, BBox(x=0.5, y=0.5, w=0.001, h=0.001)), 0.0)  # too small to measure

    def test_first_sighting(self):
        s = Scene()
        s.update(frame(T0, (4, "lamp", CENTER)), SHARP)
        [o] = s.objects()
        self.assertEqual(o, {"id": 4, "label": "lamp", "score": 0.9, "box": CENTER.model_dump(), "in_view": True,
                             "centered": 1.0, "seconds_viewed": 0.0,
                             "first_seen_ts": round(T0 * 1000), "last_seen_ts": round(T0 * 1000)})

    def test_viewing_time_and_time_weighted_centering(self):
        s = Scene()
        s.update(frame(T0, (4, "lamp", CENTER)), SHARP)
        s.update(frame(T0 + 0.2, (4, "lamp", CENTER)), SHARP)
        s.update(frame(T0 + 1.0, (4, "lamp", LEFT)), SHARP)  # 0.8 s more, the last part off to the side
        lamp = s.tracks[4]
        self.assertAlmostEqual(lamp.seconds_viewed, 1.0)
        expected = (1.0 * 0.05 + 1.0 * 0.2 + centeredness(LEFT) * 0.8) / 1.05
        self.assertAlmostEqual(lamp.centered, expected)
        self.assertEqual(lamp.box, LEFT)  # the last box

    def test_out_of_view_and_back(self):
        s = Scene()
        s.update(frame(T0, (4, "lamp", CENTER), (7, "chair", LEFT)), SHARP)
        s.update(frame(T0 + 0.5, (4, "lamp", CENTER)), SHARP)
        s.update(frame(T0 + 1.0, (2, "rug", LEFT)), SHARP)
        self.assertEqual([(o["id"], o["in_view"]) for o in s.objects()],
                         [(2, True), (4, False), (7, False)])  # in view first, then most recently seen
        self.assertEqual(s.objects()[1]["last_seen_ts"], round((T0 + 0.5) * 1000))
        s.update(frame(T0 + 3.0, (7, "chair", LEFT)), SHARP)  # the chair is back: its time away isn't viewing
        self.assertEqual(s.tracks[7].seconds_viewed, 0.0)
        self.assertTrue(s.tracks[7].in_view)

    def test_a_stall_counts_as_at_most_a_second(self):
        s = Scene()
        s.update(frame(T0, (4, "lamp", CENTER)), SHARP)
        s.update(frame(T0 + 6.0, (4, "lamp", CENTER)), SHARP)
        self.assertEqual(s.tracks[4].seconds_viewed, scene.MAX_GAP_S)

    def test_tracks_are_forgotten_after_the_window(self):
        now = [T0]
        s = Scene(clock=lambda: now[0])
        s.update(frame(T0, (4, "lamp", CENTER)), SHARP)
        s.update(frame(T0 + 1.0, (7, "chair", LEFT)), SHARP)
        now[0] = T0 + scene.WINDOW_S + 0.5
        self.assertIsNone(s.best(4))  # the lamp was last seen more than the window ago
        self.assertIsNotNone(s.best(7))
        s.update(frame(T0 + scene.WINDOW_S + 2.0), SHARP)  # nothing in view
        self.assertEqual(s.objects(), [])

    def test_the_best_frame_is_sharp_big_and_centered(self):
        s = Scene()
        big = BBox(x=0.3, y=0.3, w=0.4, h=0.4)
        s.update(frame(T0, (4, "lamp", CENTER)), BLURRY)
        s.update(frame(T0 + 0.2, (4, "lamp", CENTER)), SHARP)
        self.assertIs(s.best(4)[0], SHARP)  # sharper
        s.update(frame(T0 + 0.4, (4, "lamp", LEFT)), SHARP)
        self.assertEqual(s.best(4)[1].frame_box, CENTER)  # off to the side: not better
        bigger = checkerboard()
        s.update(frame(T0 + 0.6, (4, "lamp", big)), bigger)
        frame_, track = s.best(4)
        self.assertIs(frame_, bigger)  # bigger
        self.assertEqual((track.frame_box, track.box), (big, big))
        self.assertIsNone(s.best(99))

    def test_clear(self):
        s = Scene()
        s.update(frame(T0, (4, "lamp", CENTER)), SHARP)
        s.clear()
        self.assertEqual((s.objects(), s.best(4)), ([], None))
        s.update(frame(T0 + 0.1, (0, "sofa", CENTER)), SHARP)  # ids restart after a reset
        self.assertEqual([o["id"] for o in s.objects()], [0])


if __name__ == "__main__":
    unittest.main()
