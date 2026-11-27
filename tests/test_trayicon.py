"""The level drawn into the tray icon for hosts without labels."""

import tempfile
import unittest
import pathlib

import helpers  # noqa: F401
from luxtide import tray, trayicon


def write(base, name, text):
    (pathlib.Path(base) / name).write_text(text)


class Hosts(unittest.TestCase):
    def test_label_hosts(self):
        for process in ("gnome-shell", "ayatana-indica", "indicator-appli"):
            self.assertTrue(trayicon.label_host(process), process)
        for process in ("kded6", "plasmashell", "xfce4-panel", "xapp-sn-watcher",
                        "waybar", "lxqt-panel", ""):
            self.assertFalse(trayicon.label_host(process), process)


class PlasmaColours(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.base = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def test_nothing_configured(self):
        self.assertEqual(trayicon.plasma_colours(self.base), (trayicon.WHITE, True))

    def test_default_theme_follows_the_header_colours(self):
        write(self.base, "kdeglobals",
              "[Colors:Window]\nForegroundNormal=1,2,3\n\n"
              "[Colors:Header][Inactive]\nForegroundNormal=9,9,9\n\n"
              "[Colors:Header]\nBackgroundNormal=222,224,226\nForegroundNormal=35,38,41\n")
        self.assertEqual(trayicon.plasma_colours(self.base), ((35, 38, 41), False))

    def test_window_colours_without_header(self):
        write(self.base, "kdeglobals", "[Colors:Window]\nForegroundNormal[$i]=200,201,202\n")
        self.assertEqual(trayicon.plasma_colours(self.base), ((200, 201, 202), False))

    def test_fixed_breeze_themes(self):
        write(self.base, "kdeglobals", "[Colors:Header]\nForegroundNormal=35,38,41\n")
        write(self.base, "plasmarc", "[Theme]\nname=breeze-dark\n")
        self.assertEqual(trayicon.plasma_colours(self.base), (trayicon.WHITE, False))
        write(self.base, "plasmarc", "[Theme]\nname=breeze-light\n")
        self.assertEqual(trayicon.plasma_colours(self.base), (trayicon.DARK, False))

    def test_other_themes_get_an_outline(self):
        write(self.base, "kdeglobals", "[Colors:Header]\nForegroundNormal=35,38,41\n")
        write(self.base, "plasmarc", "[Theme]\nname=Sweet\n")
        self.assertEqual(trayicon.plasma_colours(self.base), ((35, 38, 41), True))

    def test_broken_colour(self):
        write(self.base, "kdeglobals", "[Colors:Header]\nForegroundNormal=red\n")
        self.assertEqual(trayicon.plasma_colours(self.base), (trayicon.WHITE, True))

    def test_only_plasma_reads_kde_files(self):
        write(self.base, "plasmarc", "[Theme]\nname=breeze-light\n")
        self.assertEqual(trayicon.colours("kded6", self.base), (trayicon.DARK, False))
        self.assertEqual(trayicon.colours("xfce4-panel", self.base), (trayicon.WHITE, True))
        self.assertEqual(trayicon.watched_files("xfce4-panel", self.base), [])
        self.assertEqual(len(trayicon.watched_files("kded6", self.base)), 2)


@unittest.skipUnless(trayicon.available(), "python-cairo is not installed")
class Drawing(unittest.TestCase):
    def test_every_size_in_network_byte_order(self):
        colour = (10, 120, 230)
        pixmaps = trayicon.pixmaps("54", colour, False)
        self.assertEqual([p[0] for p in pixmaps], list(trayicon.SIZES))
        for width, height, data in pixmaps:
            self.assertEqual(width, height)
            self.assertEqual(len(data), width * height * 4)
            opaque = [data[i + 1:i + 4] for i in range(0, len(data), 4) if data[i] == 255]
            self.assertTrue(opaque, width)
            self.assertTrue(all(tuple(p) == colour for p in opaque))
            edges = [data[i:i + 4] for i in range(0, len(data), 4) if 0 < data[i] < 255]
            for a, r, g, b in edges:      # anti-aliased edges keep the colour,
                # within what 8-bit premultiplication leaves of it
                self.assertLessEqual(max(abs(r - 10), abs(g - 120), abs(b - 230)),
                                     255 // a + 1)

    def test_three_digits_fit(self):
        _, _, data = trayicon.render("100", 22, trayicon.WHITE, True)
        columns = {(i // 4) % 22 for i in range(0, len(data), 4) if data[i]}
        rows = {(i // 4) // 22 for i in range(0, len(data), 4) if data[i]}
        self.assertGreater(min(columns), 0)
        self.assertLess(max(columns), 21)
        self.assertGreater(min(rows), 0)
        self.assertLess(max(rows), 21)


def bare_tray(status, host):
    item = tray.Tray.__new__(tray.Tray)
    item.status = status
    item._host = host
    return item


class LevelDisplay(unittest.TestCase):
    STATUS = {"level": 54, "mode": "auto", "phase": "day", "show_level": True}

    def test_off(self):
        item = bare_tray(dict(self.STATUS, show_level=False), "kded6")
        self.assertEqual(item._level_display(), ("", []))

    def test_label_next_to_the_icon_on_gnome(self):
        item = bare_tray(self.STATUS, "gnome-shell")
        self.assertEqual(item._level_display(), ("54%", []))

    def test_label_until_the_host_is_known(self):
        self.assertEqual(bare_tray(self.STATUS, None)._level_display(), ("54%", []))

    @unittest.skipUnless(trayicon.available(), "python-cairo is not installed")
    def test_number_as_the_icon_elsewhere(self):
        for host in ("kded6", "xfce4-panel", "waybar", ""):
            label, pixmaps = bare_tray(self.STATUS, host)._level_display()
            self.assertEqual(label, "", host)
            self.assertEqual(len(pixmaps), len(trayicon.SIZES), host)

    def test_keys_level_wins(self):
        item = bare_tray(dict(self.STATUS, shown=70), "gnome-shell")
        self.assertEqual(item._level_display(), ("70%", []))

    def test_no_number_with_broken_settings(self):
        item = bare_tray(dict(self.STATUS, config_error="bad"), "gnome-shell")
        self.assertEqual(item._level_display(), ("", []))


if __name__ == "__main__":
    unittest.main()
