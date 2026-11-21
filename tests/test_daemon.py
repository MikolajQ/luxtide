"""Daemon decisions that need no bus: following the location, weather gating."""

import pathlib
import tempfile
import types
import unittest

import helpers  # noqa: F401
from helpers import make_config
from luxtide import config as config_module, locate
from luxtide.daemon import Daemon


def stub(cfg):
    calls = []
    return types.SimpleNamespace(cfg=cfg, _locator=object(), _follow_error="",
                                 publish=lambda *a, **k: calls.append("publish"),
                                 calls=calls)


class FollowingTheLocation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "config.ini"

    def daemon(self, lat=54.35, lon=18.65):
        cfg = make_config()
        cfg.latitude, cfg.longitude = lat, lon
        cfg.location_follow = True
        cfg.source = self.path
        return stub(cfg)

    def test_rough_fix_keeps_the_saved_location(self):
        d = self.daemon()
        Daemon._follow_done(d, locate.Fix(52.23, 21.01, 25000), None)
        self.assertEqual((d.cfg.latitude, d.cfg.longitude), (54.35, 18.65))
        self.assertFalse(self.path.exists())
        self.assertIsNone(d._locator)

    def test_good_fix_elsewhere_is_saved(self):
        d = self.daemon()
        Daemon._follow_done(d, locate.Fix(50.0614, 19.9366, 35), None)
        saved = config_module.load(self.path)
        self.assertEqual((saved.latitude, saved.longitude), (50.0614, 19.9366))
        self.assertEqual(saved.location_method, "geoclue")
        self.assertEqual(saved.location_accuracy, 35)
        self.assertTrue(saved.location_follow)

    def test_small_move_is_not_saved(self):
        d = self.daemon()
        Daemon._follow_done(d, locate.Fix(54.3520, 18.6510, 35), None)
        self.assertFalse(self.path.exists())

    def test_error_is_reported_in_the_status(self):
        d = self.daemon()
        Daemon._follow_done(d, None, "The location service refused.")
        self.assertEqual(d._follow_error, "The location service refused.")
        self.assertIn("publish", d.calls)
        Daemon._follow_done(d, locate.Fix(50.0614, 19.9366, 35), None)
        self.assertEqual(d._follow_error, "")


class WeatherNeedsALocation(unittest.TestCase):
    def test_blocked_with_the_reference_city(self):
        cfg = make_config("[weather]\nenabled = yes\n")
        d = types.SimpleNamespace(cfg=cfg, schedule=types.SimpleNamespace(
            location_source="timezone"))
        self.assertTrue(Daemon.weather_blocked(d))
        d.schedule.location_source = "config"
        self.assertFalse(Daemon.weather_blocked(d))
        cfg.weather_enabled = False
        d.schedule.location_source = "timezone"
        self.assertFalse(Daemon.weather_blocked(d))


if __name__ == "__main__":
    unittest.main()
