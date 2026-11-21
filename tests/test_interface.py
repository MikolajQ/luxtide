"""Tray menu model and command-line parsing."""

import datetime as dt
import pathlib
import time
import unittest

import helpers  # noqa: F401
from luxtide import cli, policy, tray


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, args))


def labels(item):
    found = []
    for child in item.children:
        if not child.visible:
            continue
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

    def test_location_entry_only_without_a_location(self):
        self.assertNotIn("Set your location…", labels(tray.build_menu(STATUS, Recorder(), {})))
        actions = Recorder()
        root = tray.build_menu(dict(STATUS, location_source="timezone"), actions, {})
        find(root, "Set your location…").action()
        self.assertEqual(actions.calls[-1], ("open_locate", ()))

    def test_brighter_and_dimmer_stop_at_the_limits(self):
        top = tray.build_menu(dict(STATUS, level=100), Recorder(), {})
        self.assertFalse(find(top, "Brighter").enabled)
        self.assertTrue(find(top, "Dimmer").enabled)
        bottom = tray.build_menu(dict(STATUS, level=1), Recorder(), {})
        self.assertFalse(find(bottom, "Dimmer").enabled)
        actions = Recorder()
        find(tray.build_menu(STATUS, actions, {}), "Dimmer").action()
        self.assertEqual(actions.calls[-1], ("step", (-1,)))


class ExtensionClient:
    """The caching rules of GNOME's AppIndicator extension (dbusMenu.js).

    On LayoutUpdated it fetches the tree with only "type" and
    "children-display"; items it already knows keep every other property
    unless ItemsPropertiesUpdated said otherwise. New items are read in full.
    """

    def __init__(self, model):
        self.model = model
        self.props = {}
        self.children = {}
        self.fetch_layout()

    def fetch_layout(self):
        seen = set()

        def walk(item):
            seen.add(item.id)
            full = item.properties()
            if item.id in self.props:
                for name in ("type", "children-display"):
                    if name in full:
                        self.props[item.id][name] = full[name].unpack()
            else:
                self.props[item.id] = {k: v.unpack() for k, v in full.items()}
            self.children[item.id] = [child.id for child in item.children]
            for child in item.children:
                walk(child)

        walk(self.model.root)
        for gone in set(self.props) - seen:
            del self.props[gone]

    def apply(self, reshaped, updated, removed):
        if reshaped:
            self.fetch_layout()
        for item_id, changed in updated:
            if item_id in self.props:
                self.props[item_id].update({k: v.unpack() for k, v in changed.items()})
        for item_id, names in removed:
            for name in names:
                self.props.get(item_id, {}).pop(name, None)

    def shown(self, item_id=0):
        """Labels as drawn, depth first, with the id a click would send."""
        rows = []
        for child in self.children.get(item_id, []):
            p = self.props[child]
            if not p.get("visible", True) or p.get("type") == "separator":
                continue
            rows.append((p.get("label", ""), child))
            rows += self.shown(child)
        return rows


class TrayMenuUpdates(unittest.TestCase):
    STATES = [
        STATUS,
        dict(STATUS, mode="override", level=75, until=2_000_000_000),
        dict(STATUS, mode="override", level=50, until=2_000_000_000, weather_enabled=False),
        dict(STATUS, mode="paused", level=50, until=2_000_000_000),
        dict(STATUS, mode="paused", level=50, until=0, sunrise=0),
        dict(STATUS, outputs=[{"label": "Monitor", "state": "failed"}]),
        {"config_error": "bad"},
        dict(STATUS, level=99, next_change=0),
    ]

    def test_extension_shows_what_we_mean(self):
        model = tray.MenuModel()
        model.update(tray.build_menu(self.STATES[0], Recorder(), {}))
        client = ExtensionClient(model)
        for status in self.STATES[1:] + self.STATES[:1]:
            root = tray.build_menu(status, Recorder(), {})
            client.apply(*model.update(root))
            for label, item_id in client.shown():
                with self.subTest(status=status, label=label):
                    self.assertEqual(model.items[item_id].label.replace("_", "__"), label)
            self.assertEqual([label for label, _ in client.shown()], labels(root))

    def test_clicks_reach_the_item_shown(self):
        model = tray.MenuModel()
        actions = Recorder()
        model.update(tray.build_menu(STATUS, actions, {}))
        client = ExtensionClient(model)
        client.apply(*model.update(tray.build_menu(self.STATES[1], actions, {})))
        for label, level in (("75 %", 75), ("10 %", 10)):
            item_id = dict(client.shown())[label]
            model.items[item_id].action()
            self.assertEqual(actions.calls[-1], ("set_level", (level, policy.NEXT_PHASE)))

    def test_mode_changes_keep_the_shape(self):
        model = tray.MenuModel()
        model.update(tray.build_menu(STATUS, Recorder(), {}))
        for status in self.STATES[1:5]:
            reshaped, updated, _ = model.update(tray.build_menu(status, Recorder(), {}))
            self.assertFalse(reshaped, status)
            self.assertTrue(updated)
        reshaped, _, _ = model.update(tray.build_menu(self.STATES[4], Recorder(), {}))
        self.assertFalse(reshaped)

    def test_ids_survive_a_round_trip(self):
        model = tray.MenuModel()
        model.update(tray.build_menu(STATUS, Recorder(), {}))
        before = {item.label: i for i, item in model.items.items()}
        model.update(tray.build_menu({"config_error": "bad"}, Recorder(), {}))
        model.update(tray.build_menu(STATUS, Recorder(), {}))
        after = {item.label: i for i, item in model.items.items()}
        self.assertEqual(before, after)

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


class DaemonLogging(unittest.TestCase):
    def test_log_file_without_journal(self):
        import io
        import logging
        import os
        import tempfile
        from unittest import mock
        root = logging.getLogger()
        before = list(root.handlers)
        with tempfile.TemporaryDirectory() as state, \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": state}), \
                mock.patch.object(cli.sys, "stderr", io.StringIO()):
            os.environ.pop("JOURNAL_STREAM", None)
            cli.setup_daemon_logging(False)
            added = [h for h in root.handlers if h not in before]
            try:
                files = [h for h in added if isinstance(h, logging.FileHandler)]
                self.assertEqual(len(files), 1)
                logging.getLogger("luxtide").warning("hello")
                files[0].flush()
                text = (pathlib.Path(state) / "luxtide" / "daemon.log").read_text()
                self.assertIn("hello", text)
            finally:
                for handler in added:
                    root.removeHandler(handler)
                    handler.close()


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
