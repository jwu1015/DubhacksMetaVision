"""Tests for trigger.py: the quick label and Look timers."""
from storefront.live.trigger import Dwell


def test_each_timer_fires_once_after_its_time_on_screen():
    d = Dwell(label_ms=500, look_ms=1500)
    assert d.update(["a"], 0.0) == ([], [])  # just appeared: could be flicker
    assert d.update(["a"], 0.6) == (["a"], [])
    assert d.update(["a"], 1.0) == ([], [])  # labeled already
    assert d.update(["a"], 1.5) == ([], ["a"])
    assert d.update(["a"], 9.0) == ([], [])


def test_equal_timers_fire_together():
    d = Dwell(label_ms=500, look_ms=500)
    d.update(["a", "b"], 0.0)
    assert d.update(["a", "b"], 0.5) == (["a", "b"], ["a", "b"])


def test_leaving_the_screen_resets_the_timers():
    d = Dwell(label_ms=500, look_ms=1000)
    d.update(["a"], 0.0)
    d.update(["a"], 0.6)                     # labeled
    d.update([], 0.8)                        # left before its Look was due
    assert d.update(["a"], 1.2) == ([], [])  # back: its Look timer starts over
    assert d.update(["a"], 2.2) == ([], ["a"])


def test_a_look_started_another_way_isnt_started_again():
    d = Dwell(label_ms=500, look_ms=500)
    d.update(["a"], 0.0)
    d.looked("a")
    assert d.update(["a"], 1.0) == (["a"], [])


def test_reset_forgets_everything():
    d = Dwell(label_ms=0, look_ms=0)
    assert d.update(["a"], 0.0) == (["a"], ["a"])
    d.reset()
    assert d.update(["a"], 0.0) == (["a"], ["a"])
