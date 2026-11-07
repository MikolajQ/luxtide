import unittest

from helpers import WARSAW, make_config
from ddc_sun_dimmer import config

# The shape of a version 1 file, as written before the rewrite.
LEGACY = """\
[location]
latitude = 50.0
longitude = 20.0
timezone = Europe/Warsaw

[levels]
day = 100
twilight = 75
night = 1

[schedule]
smooth = yes
margin_minutes = 60
curve = perceptual
wake_window_seconds = 90
refresh_minutes = 5

[ddc]
bus =
sleep_multiplier = 2
retries = 5
repeats = 3
settle = 0.2
syslog = NEVER
mccs = 2.0

[night_light]
sync = no
"""


class Parsing(unittest.TestCase):
    def test_legacy_file_keeps_its_behaviour(self):
        cfg = config.parse(LEGACY)
        self.assertEqual((cfg.day, cfg.twilight, cfg.night), (100, 75, 1))
        self.assertEqual(cfg.curve_shape, 1.0)
        self.assertEqual(cfg.ddc.refresh_minutes, 5)
        self.assertTrue(cfg.ddc.wake_burst)
        self.assertEqual((cfg.ddc.repeats, cfg.ddc.retries), (3, 5))
        self.assertEqual(cfg.ddc.sleep_multiplier, 2.0)
        self.assertEqual(cfg.ddc.mccs, "2.0")
        linear = config.parse(LEGACY.replace("curve = perceptual", "curve = linear"))
        self.assertEqual(linear.curve_shape, 0.0)

    def test_render_round_trip(self):
        cfg = make_config("[weather]\nenabled = yes\nstrength = 35\n"
                          "[schedule]\nmode = elevation\ncurve_shape = 0.67\n"
                          "[tray]\npause_presets = 10, 45, 90\n"
                          "[display:SAM-SMBX2231]\noffset = -5\nrepeats = 4\nwake_burst = yes\n"
                          "[display:internal]\nenabled = no\n")
        again = config.parse(config.render(cfg))
        self.assertEqual(again, cfg)
        self.assertEqual(again.ddc_for("SAM-SMBX2231").repeats, 4)
        self.assertTrue(again.ddc_for("SAM-SMBX2231").wake_burst)
        self.assertEqual(again.ddc_for("other").repeats, 1)
        self.assertEqual(again.tray_pause_presets, [10, 45, 90])

    def test_errors_name_the_setting(self):
        cases = {
            "[levels]\nday = 0\n": "[levels] day",
            "[levels]\nnight = lots\n": "[levels] night",
            "[schedule]\nmode = moon\n": "[schedule] mode",
            "[schedule]\nmode = elevation\nnight_elevation = 3\nday_elevation = 2\n":
                "night_elevation",
            "[weather]\nrefresh_minutes = 1\n": "[weather] refresh_minutes",
            "[tray]\npause_presets = 10, soon\n": "[tray] pause_presets",
            "[display:x]\nminimum = 50\nmaximum = 40\n": "[display:x]",
        }
        for text, needle in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(config.ConfigError) as caught:
                    make_config(text)
                self.assertIn(needle, str(caught.exception))

    def test_half_a_location_is_an_error(self):
        with self.assertRaises(config.ConfigError):
            config.parse("[location]\nlatitude = 50\n")

    def test_location_from_time_zone(self):
        cfg = config.parse("[location]\ntimezone = Europe/Warsaw\n")
        lat, lon, source = cfg.location()
        self.assertEqual(source, "timezone")
        self.assertAlmostEqual(lat, 52.25)

    def test_render_mentions_every_section(self):
        text = config.render(make_config())
        for section in ("location", "levels", "schedule", "weather", "ddc", "backlight",
                        "tray"):
            self.assertIn(f"[{section}]", text)
        self.assertTrue(text.startswith("#"))
        self.assertIn(WARSAW.splitlines()[1], text)


if __name__ == "__main__":
    unittest.main()
