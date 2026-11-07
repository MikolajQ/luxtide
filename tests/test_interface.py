"""Tray menu model and command-line parsing."""

import datetime as dt
import time
import unittest

import helpers  # noqa: F401
from ddc_sun_dimmer import cli, policy, tray


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, args))


def labels(item):
    found = []
    for child in item.children:
        if not child.separator:
            found.append(child.label)
        found += labels(child)
    return found


def find(item, label):
    for child in item.children:
        if child.label == label:
            return child
        hit = find(child, label)
        if hit:
            return hit
    return None


STATUS = {"level": 37, "mode": "auto", "phase": "dusk", "next_change": 1_800_000_000,
          "next_level": 30, "until": 0, "pause_presets": [15, 30, 60, 120],
          "sunrise": 1_799_960_000, "sunset": 1_799_990_000, "weather_enabled": True,
          "cloud": 80.0, "factor": 0.9, "next_phase": 1_800_001_000, "outputs": []}


class TrayMenu(unittest.TestCase):
    def test_auto(self):
        actions = Recorder()
        root = tray.build_menu(STATUS, actions, {})
        texts = labels(root)
        self.assertEqual(texts[0], "Brightness 37 %")
        self.assertIn("Cloud cover 80 %, -10 %", texts)
        self.assertNotIn("Back to schedule", texts)
        find(root, "30 minutes").action()
        name, (until,) = actions.calls[-1]
        self.assertEqual(name, "pause")
        self.assertAlmostEqual(until, time.time() + 1800, delta=2)
        find(root, "50 %").action()
        self.assertEqual(actions.calls[-1], ("set_level", (50, policy.NEXT_PHASE)))
        find(root, "Until resumed").action()
        self.assertEqual(actions.calls[-1], ("pause", (policy.FOREVER,)))

    def test_paused_offers_resume_and_extension(self):
        status = dict(STATUS, mode="paused", until=2_000_000_000)
        actions = Recorder()
        root = tray.build_menu(status, actions, {"last_pause_minutes": 45})
        texts = labels(root)
        self.assertTrue(texts[0].startswith("Paused at 37 %"))
        self.assertIn("Resume now", texts)
        self.assertIn("45 minutes (last used)", texts)
        find(root, "Extend pause by 15 min").action()
        self.assertEqual(actions.calls[-1], ("pause", (2_000_000_000 + 900,)))

    def test_override_offers_back_to_schedule(self):
        root = tray.build_menu(dict(STATUS, mode="override", until=2_000_000_000),
                               Recorder(), {})
        self.assertIn("Back to schedule", labels(root))

    def test_config_error(self):
        root = tray.build_menu({"config_error": "bad"}, Recorder(), {})
        self.assertEqual(labels(root), ["Settings error", "bad", "Open settings…"])

    def test_ids_are_unique(self):
        root = tray.build_menu(STATUS, Recorder(), {})
        table = tray.number_items(root)
        self.assertEqual(sorted(table), list(range(len(table))))
        self.assertIs(table[0], root)

    def test_durations_and_icons(self):
        self.assertEqual(tray.duration_label(45), "45 minutes")
        self.assertEqual(tray.duration_label(60), "1 hour")
        self.assertEqual(tray.duration_label(150), "2 h 30 min")
        self.assertEqual(tray.icon_name(dict(STATUS, mode="paused")),
                         "media-playback-pause-symbolic")
        self.assertEqual(tray.icon_name(dict(STATUS, phase="night")),
                         "weather-clear-night-symbolic")
        self.assertEqual(tray.icon_name(dict(STATUS, phase="day")),
                         "weather-few-clouds-symbolic")


class Durations(unittest.TestCase):
    def test_parse_when(self):
        now = dt.datetime(2026, 11, 7, 14, 0).astimezone().timestamp()
        self.assertEqual(cli.parse_when("30m", now), now + 1800)
        self.assertEqual(cli.parse_when("90", now), now + 5400)
        self.assertEqual(cli.parse_when("1h30m", now), now + 5400)
        self.assertEqual(cli.parse_when("2h", now), now + 7200)
        self.assertEqual(cli.parse_when("1h 15", now), now + 4500)
        self.assertEqual(cli.parse_when("15:30", now), now + 5400)
        self.assertEqual(cli.parse_when("13:00", now), now + 23 * 3600)    # tomorrow
        self.assertEqual(cli.parse_when("phase", now), policy.NEXT_PHASE)
        self.assertEqual(cli.parse_when("", now), policy.FOREVER)
        for bad in ("soon", "25:00", "0m", "h"):
            with self.assertRaises(ValueError, msg=bad):
                cli.parse_when(bad, now)


if __name__ == "__main__":
    unittest.main()
