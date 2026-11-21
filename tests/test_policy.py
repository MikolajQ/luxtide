import datetime as dt
import unittest

from helpers import make_config
from luxtide import curve, policy, weather


class Weather(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config("[levels]\nday = 100\ntwilight = 75\nnight = 10\n"
                               "[weather]\nenabled = yes\nstrength = 20\n")
        self.schedule = curve.Schedule(self.cfg)
        self.noon = dt.datetime(2026, 11, 7, 12, tzinfo=self.schedule.tz).timestamp()
        self.night = dt.datetime(2026, 11, 7, 23, tzinfo=self.schedule.tz).timestamp()

    def test_full_cloud_at_noon(self):
        level, base, factor = policy.scheduled(self.schedule, self.noon, 100, self.cfg)
        self.assertEqual((level, base), (80, 100))
        self.assertAlmostEqual(factor, 0.8)

    def test_partial_cloud(self):
        level, _, _ = policy.scheduled(self.schedule, self.noon, 50, self.cfg)
        self.assertEqual(level, 90)

    def test_no_effect_at_night(self):
        level, _, factor = policy.scheduled(self.schedule, self.night, 100, self.cfg)
        self.assertEqual(level, 10)
        self.assertAlmostEqual(factor, 1.0)

    def test_disabled_or_unknown(self):
        self.assertEqual(policy.scheduled(self.schedule, self.noon, None, self.cfg)[0], 100)
        self.cfg.weather_enabled = False
        self.assertEqual(policy.scheduled(self.schedule, self.noon, 100, self.cfg)[0], 100)

    def test_never_below_night(self):
        self.cfg.weather_strength = 90
        self.cfg.day = 12
        level, _, _ = policy.scheduled(curve.Schedule(self.cfg), self.noon, 100, self.cfg)
        self.assertEqual(level, 10)

    def test_correction_formula(self):
        self.assertEqual(weather.correction(100, None, 20, 1), 1.0)
        self.assertAlmostEqual(weather.correction(100, 100, 20, 0.5), 0.9)
        self.assertAlmostEqual(weather.correction(100, 150, 20, 1), 0.8)


class Holds(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config("[levels]\nday = 100\ntwilight = 75\nnight = 10\n")
        self.schedule = curve.Schedule(self.cfg)
        self.now = dt.datetime(2026, 11, 7, 12, tzinfo=self.schedule.tz).timestamp()

    def decide(self, state, now=None):
        return policy.decide(self.schedule, state, now or self.now, None, self.cfg)

    def test_priority(self):
        state = policy.State()
        self.assertEqual(self.decide(state).mode, "auto")
        policy.set_level(state, 40, self.now + 600)
        self.assertEqual((self.decide(state).level, self.decide(state).mode), (40, "override"))
        policy.pause(state, 40, None)
        self.assertEqual((self.decide(state).level, self.decide(state).mode), (40, "paused"))
        self.assertIsNone(state.override)
        policy.preview(state, 5, self.now)
        self.assertEqual((self.decide(state).level, self.decide(state).mode), (5, "preview"))
        policy.resume(state)
        self.assertEqual(self.decide(state).mode, "auto")

    def test_manual_level_inside_pause(self):
        state = policy.State()
        policy.pause(state, 100, self.now + 1800)
        policy.set_level(state, 30, None)
        self.assertEqual(state.pause.level, 30)
        self.assertIsNone(state.override)
        self.assertEqual(state.pause.until, self.now + 1800)

    def test_pausing_again_moves_the_end(self):
        state = policy.State()
        policy.pause(state, 60, self.now + 900)
        policy.pause(state, 99, self.now + 3600)
        self.assertEqual((state.pause.level, state.pause.until), (60, self.now + 3600))

    def test_expiry(self):
        state = policy.State()
        policy.set_level(state, 40, self.now + 60)
        self.assertFalse(state.expire(self.now + 59))
        self.assertTrue(state.expire(self.now + 60))
        self.assertIsNone(state.override)

    def test_resolve_until(self):
        self.assertIsNone(policy.resolve_until(policy.FOREVER, self.now, self.schedule))
        change = policy.resolve_until(policy.NEXT_PHASE, self.now, self.schedule)
        self.assertEqual(change, self.schedule.next_phase_change(self.now))
        self.assertEqual(policy.resolve_until(self.now + 5, self.now, self.schedule),
                         self.now + 5)
        with self.assertRaises(ValueError):
            policy.resolve_until(self.now - 5, self.now, self.schedule)

    def test_state_round_trip(self):
        state = policy.State(override=policy.Hold(40, self.now + 600),
                             pause=policy.Hold(55, None),
                             preview=policy.Hold(3, self.now + 10))
        restored = policy.State.from_json(state.to_json(), self.now)
        self.assertEqual(restored.override, state.override)
        self.assertEqual(restored.pause, state.pause)
        self.assertIsNone(restored.preview)      # previews never survive a restart
        expired = policy.State.from_json(state.to_json(), self.now + 601)
        self.assertIsNone(expired.override)
        self.assertEqual(policy.State.from_json("garbage", self.now), policy.State())

    def test_next_change(self):
        state = policy.State()
        when, level = policy.next_change(self.schedule, state, self.now, lambda t: None, self.cfg)
        self.assertEqual(level, 99)
        self.assertAlmostEqual(when, self.schedule.next_phase_change(self.now), delta=120)
        policy.set_level(state, 40, self.now + 600)
        self.assertEqual(policy.next_change(self.schedule, state, self.now, lambda t: None,
                                            self.cfg), (self.now + 600, 100))
        policy.resume(state)
        policy.pause(state, 40, None)
        self.assertEqual(policy.next_change(self.schedule, state, self.now, lambda t: None,
                                            self.cfg), (None, None))


if __name__ == "__main__":
    unittest.main()
