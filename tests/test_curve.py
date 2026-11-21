import datetime as dt
import math
import unittest

from helpers import make_config
from luxtide import clamp_level, curve


def at(schedule, date, hour, minute=0):
    return dt.datetime.combine(date, dt.time(hour, minute), schedule.tz).timestamp()


class Interpolation(unittest.TestCase):
    def test_endpoints_and_midpoints(self):
        for shape in (0.0, 0.3, 0.67, 1.0):
            self.assertAlmostEqual(curve.interpolate(75, 1, 0, shape), 75)
            self.assertAlmostEqual(curve.interpolate(75, 1, 1, shape), 1)
        self.assertAlmostEqual(curve.interpolate(100, 1, 0.5, 0.0), 50.5)
        self.assertAlmostEqual(curve.interpolate(100, 1, 0.5, 1.0), 10.0)
        # Cube root sits in between.
        middle = curve.interpolate(100, 1, 0.5, 2 / 3)
        self.assertTrue(10 < middle < 50.5)

    def test_progress_is_clamped(self):
        self.assertAlmostEqual(curve.interpolate(10, 20, 1.5, 0), 20)
        self.assertAlmostEqual(curve.interpolate(10, 20, -1, 0), 10)


class TimeMode(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config("[levels]\nday = 100\ntwilight = 75\nnight = 1\n")
        self.schedule = curve.Schedule(self.cfg)
        self.date = dt.date(2026, 11, 7)
        self.sun = self.schedule.sun(self.date)

    def test_anchor_levels(self):
        s = self.schedule
        rise, set_ = self.sun.rise.timestamp(), self.sun.set.timestamp()
        self.assertAlmostEqual(s.base(rise), 75)
        self.assertAlmostEqual(s.base(set_), 75)
        self.assertAlmostEqual(s.base(rise - 3600), 1)
        self.assertAlmostEqual(s.base(rise + 3600), 100)
        self.assertAlmostEqual(s.base(at(s, self.date, 12)), 100)
        self.assertAlmostEqual(s.base(at(s, self.date, 23)), 1)

    def test_morning_ramp_is_monotonic(self):
        rise = self.sun.rise.timestamp()
        levels = [self.schedule.base(rise - 3600 + m * 60) for m in range(121)]
        self.assertEqual(levels, sorted(levels))

    def test_phases_and_next_change(self):
        s = self.schedule
        rise, set_ = self.sun.rise.timestamp(), self.sun.set.timestamp()
        self.assertEqual(s.phase(rise - 7200), "night")
        self.assertEqual(s.phase(rise), "dawn")
        self.assertEqual(s.phase(rise + 7200), "day")
        self.assertEqual(s.phase(set_), "dusk")
        self.assertAlmostEqual(s.next_phase_change(rise - 7200), rise - 3600)
        self.assertAlmostEqual(s.next_phase_change(rise - 600), rise + 3600)
        self.assertAlmostEqual(s.next_phase_change(at(s, self.date, 12)), set_ - 3600)

    def test_step_mode(self):
        cfg = make_config("[levels]\nday = 100\ntwilight = 60\nnight = 20\n"
                          "[schedule]\nsmooth = no\n")
        s = curve.Schedule(cfg)
        rise = s.sun(self.date).rise.timestamp()
        self.assertEqual(s.base(rise - 3601), 20)
        self.assertEqual(s.base(rise - 3599), 60)
        self.assertEqual(s.base(rise + 3601), 100)

    def test_short_days_never_cross_ramps(self):
        # Just south of the polar circle in December: a day of about two hours,
        # shorter than two margins.
        cfg = make_config("[levels]\nday = 100\ntwilight = 50\nnight = 5\n"
                          "[schedule]\nmargin_minutes = 120\n",
                          base="[location]\nlatitude = 66.0\nlongitude = 25.0\n"
                               "timezone = Europe/Helsinki\n")
        s = curve.Schedule(cfg)
        date = dt.date(2026, 12, 21)
        points = s.anchors(date)
        self.assertEqual([p[0] for p in points], sorted(p[0] for p in points))
        sun = s.sun(date)
        self.assertIsNotNone(sun.rise)
        noon = (sun.rise.timestamp() + sun.set.timestamp()) / 2
        self.assertAlmostEqual(s.base(noon), 100)
        for minute in range(0, 24 * 60, 10):
            level = s.base(at(s, date, 0) + minute * 60)
            self.assertTrue(5 <= level <= 100)

    def test_polar_night_stays_dark(self):
        cfg = make_config("[levels]\nnight = 7\n",
                          base="[location]\nlatitude = 78.2\nlongitude = 15.6\n"
                               "timezone = Arctic/Longyearbyen\n")
        for mode in ("time", "elevation"):
            cfg.mode = mode
            s = curve.Schedule(cfg)
            date = dt.date(2026, 12, 21)
            for hour in range(24):
                self.assertAlmostEqual(s.base(at(s, date, hour)), 7, msg=f"{mode} {hour}")


class ElevationMode(unittest.TestCase):
    def test_levels_follow_the_sun(self):
        cfg = make_config("[levels]\nday = 90\ntwilight = 40\nnight = 10\n"
                          "[schedule]\nmode = elevation\n")
        s = curve.Schedule(cfg)
        date = dt.date(2026, 6, 21)
        self.assertAlmostEqual(s.base(at(s, date, 13)), 90)
        self.assertAlmostEqual(s.base(at(s, date, 1)), 10)
        # Exactly at the twilight elevation the level is the twilight level.
        start = at(s, date, 0)
        moment = start
        while s.elevation(moment) < cfg.twilight_elevation:
            moment += 10
        self.assertAlmostEqual(s.base(moment), 40, delta=0.5)
        self.assertEqual(s.phase(moment), "dawn")
        change = s.next_phase_change(start)
        self.assertLess(s.elevation(change - 2), cfg.night_elevation + 0.01)
        self.assertNotEqual(s.phase(change), "night")

    def test_daylight_weight(self):
        cfg = make_config("[levels]\nday = 100\ntwilight = 75\nnight = 1\n")
        s = curve.Schedule(cfg)
        self.assertEqual(s.daylight_weight(1), 0)
        self.assertEqual(s.daylight_weight(100), 1)
        self.assertAlmostEqual(s.daylight_weight(10), math.log(10) / math.log(100))

    def test_profile_covers_the_day(self):
        cfg = make_config()
        s = curve.Schedule(cfg)
        profile = s.profile(dt.date(2026, 3, 29), 30)   # the spring DST change
        self.assertEqual(len(profile), 23 * 2 + 1)
        self.assertTrue(all(1 <= clamp_level(level) <= 100 for _, level in profile))


if __name__ == "__main__":
    unittest.main()
