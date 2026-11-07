"""Display discovery and actuators against fake sysfs trees and fake buses."""

import os
import pathlib
import tempfile
import unittest

from helpers import edid
from ddc_sun_dimmer import displays
from ddc_sun_dimmer.backends import backlight
from ddc_sun_dimmer.config import DdcSettings


class FakeSysfs:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.drm = self.root / "drm"
        self.backlight = self.root / "backlight"
        self.drm.mkdir()
        self.backlight.mkdir()

    def connector(self, name, connected=True, dpms="On", bus=None, monitor=None):
        path = self.drm / name
        path.mkdir()
        (path / "status").write_text("connected\n" if connected else "disconnected\n")
        (path / "dpms").write_text(dpms + "\n")
        (path / "edid").write_bytes(monitor or b"")
        if bus is not None:
            target = self.root / f"i2c-{bus}"
            target.mkdir(exist_ok=True)
            os.symlink(target, path / "ddc")
        return path

    def device(self, name, kind, maximum, value):
        path = self.backlight / name
        path.mkdir()
        (path / "type").write_text(kind + "\n")
        (path / "max_brightness").write_text(f"{maximum}\n")
        (path / "brightness").write_text(f"{value}\n")
        (path / "actual_brightness").write_text(f"{value}\n")
        return path


class Discovery(unittest.TestCase):
    def test_edid(self):
        self.assertEqual(displays.parse_edid(edid("SAM", "SMBX2231", "H9XZ")),
                         ("SAM", "SMBX2231", "H9XZ"))
        self.assertEqual(displays.parse_edid(b"junk"), (None, None, None))

    def test_connectors(self):
        fs = FakeSysfs()
        fs.connector("card1-DVI-D-1", bus=5, monitor=edid("SAM", "SMBX2231"))
        fs.connector("card1-HDMI-A-1", connected=False, bus=6)
        fs.connector("card0-eDP-1", dpms="Off")
        found = {c.name: c for c in displays.list_connectors(fs.drm)}
        dvi = found["card1-DVI-D-1"]
        self.assertEqual((dvi.bus, dvi.key, dvi.short), (5, "SAM-SMBX2231", "DVI-D-1"))
        self.assertTrue(dvi.awake())
        self.assertFalse(found["card1-HDMI-A-1"].connected)
        self.assertTrue(found["card0-eDP-1"].internal)
        self.assertEqual(found["card0-eDP-1"].key, "internal")
        self.assertFalse(found["card0-eDP-1"].awake())

    def test_identical_monitors_get_distinct_keys(self):
        fs = FakeSysfs()
        fs.connector("card1-DP-1", bus=3, monitor=edid("DEL", "U2720Q", "AAA"))
        fs.connector("card1-DP-2", bus=4, monitor=edid("DEL", "U2720Q", "BBB"))
        keys = sorted(c.key for c in displays.list_connectors(fs.drm))
        self.assertEqual(keys, ["DEL-U2720Q-AAA", "DEL-U2720Q-BBB"])

    def test_backlight_choice(self):
        fs = FakeSysfs()
        connectors = [displays.Connector("card0-eDP-1", fs.connector("card0-eDP-1"),
                                         True, None)]
        fs.device("intel_backlight", "raw", 96000, 48000)
        fs.device("acpi_video0", "firmware", 15, 7)
        fs.device("broken", "raw", 0, 0)
        devices = displays.list_backlights(fs.backlight, connectors)
        self.assertEqual(sorted(d.name for d in devices), ["acpi_video0", "intel_backlight"])
        self.assertEqual(displays.pick_backlight(devices).name, "acpi_video0")
        self.assertEqual(displays.pick_backlight(devices, "intel_backlight").name,
                         "intel_backlight")
        self.assertIsNone(displays.pick_backlight(devices, "missing"))
        intel = next(d for d in devices if d.name == "intel_backlight")
        self.assertEqual(intel.read_raw(), 48000)
        self.assertEqual(intel.connector.short, "eDP-1")


class Scaling(unittest.TestCase):
    def test_percent_to_raw_never_off(self):
        self.assertEqual(backlight.percent_to_raw(1, 7), 1)
        self.assertEqual(backlight.percent_to_raw(100, 7), 7)
        self.assertEqual(backlight.percent_to_raw(50, 96000), 48000)
        self.assertEqual(backlight.percent_to_raw(1, 100, minimum=5), 6)
        self.assertEqual(backlight.raw_to_percent(48000, 96000), 50)
        self.assertEqual(backlight.raw_to_percent(0, 7), 1)


class FakeBus:
    def __init__(self, properties, owners=(), fail=()):
        self.properties = properties
        self.owners = set(owners)
        self.fail = set(fail)
        self.calls = []

    def call(self, name, path, interface, method, signature=None, args=(), reply=None):
        if method in self.fail:
            raise RuntimeError(f"{method} refused")
        if method == "GetAll":
            return (self.properties[(path, args[0])],)
        self.calls.append((name, path, method, args))
        return ()

    def get(self, name, path, interface, prop):
        return self.properties[(path, prop)]

    def has_owner(self, name):
        return name in self.owners


class Actuators(unittest.TestCase):
    def setUp(self):
        fs = FakeSysfs()
        self.fs = fs
        path = fs.device("intel_backlight", "raw", 1000, 500)
        connector = displays.Connector("card0-eDP-1", fs.connector("card0-eDP-1"), True, None)
        self.device = displays.BacklightDevice("intel_backlight", path, "raw", 1000, connector)

    def mutter_bus(self, **kw):
        entries = [{"connector": "eDP-1", "active": True, "min": 0, "max": 255, "value": 128},
                   {"connector": "DP-2"}]
        return FakeBus({(backlight.MutterMethod.PATH, "Backlight"): (7, entries)}, **kw)

    def test_mutter_first(self):
        session = self.mutter_bus()
        system = FakeBus({}, owners={"org.freedesktop.login1"})
        actuator = backlight.BacklightActuator(self.device, session_bus=session,
                                               system_bus=system)
        self.assertTrue(actuator.write(50))
        self.assertEqual(actuator.method.name, "mutter")
        self.assertEqual(session.calls[-1][2:], ("SetBacklight", (7, "eDP-1", 128)))
        self.assertEqual(actuator.read(), 50)

    def test_falls_back_to_logind(self):
        session = self.mutter_bus(fail={"SetBacklight"})
        system = FakeBus({}, owners={"org.freedesktop.login1"})
        actuator = backlight.BacklightActuator(self.device, session_bus=session,
                                               system_bus=system)
        self.assertTrue(actuator.write(30))
        self.assertEqual(actuator.method.name, "logind")
        self.assertEqual(system.calls[-1][2:], ("SetBrightness",
                                                ("backlight", "intel_backlight", 300)))
        self.assertEqual(actuator.read(), 50)       # read from sysfs

    def test_kde(self):
        path = "/org/kde/ScreenBrightness/display1"
        session = FakeBus({
            (backlight.MutterMethod.PATH, "Backlight"): (1, []),
            (backlight.KdeMethod.PATH, "DisplaysDBusNames"): ["display0", "display1"],
            ("/org/kde/ScreenBrightness/display0", backlight.KdeMethod.DISPLAY):
                {"IsInternal": False, "MaxBrightness": 100, "Brightness": 10},
            (path, backlight.KdeMethod.DISPLAY):
                {"IsInternal": True, "MaxBrightness": 400, "Brightness": 100},
        }, owners={"org.kde.ScreenBrightness"})
        actuator = backlight.BacklightActuator(self.device, session_bus=session,
                                               system_bus=FakeBus({}))
        self.assertTrue(actuator.write(75))
        self.assertEqual(actuator.method.name, "kde")
        self.assertEqual(session.calls[-1][1:], (path, "SetBrightness", (300, 1)))
        self.assertEqual(actuator.read(), 25)

    def test_nothing_available(self):
        actuator = backlight.BacklightActuator(self.device, session_bus=FakeBus({}),
                                               system_bus=FakeBus({}), method="logind")
        self.assertFalse(actuator.write(50))


class DdcCommand(unittest.TestCase):
    def test_flags(self):
        from ddc_sun_dimmer.backends import ddc
        ddc.ddcutil_major.cache_clear()
        actuator = ddc.DdcActuator(5, DdcSettings(sleep_multiplier=2.0, mccs="2.0"))
        command = actuator.command(ddc.VCP_BRIGHTNESS, 40)
        self.assertEqual(command[1:5], ["--bus", "5", "--noverify", "--sleep-multiplier"])
        self.assertEqual(command[5], "2")
        self.assertEqual(command[-3:], ["setvcp", "10", "40"])
        self.assertIn("--mccs", command)
        if ddc.ddcutil_major() >= 2:
            self.assertIn("--skip-ddc-checks", command)
            self.assertIn("--syslog=NEVER", command)

    def test_dry_run_counts_repeats(self):
        from ddc_sun_dimmer.backends import ddc
        actuator = ddc.DdcActuator(5, DdcSettings(repeats=3, settle=0), dry_run=True)
        self.assertEqual(actuator.set_vcp(ddc.VCP_CONTRAST, 60), (3, 3))


if __name__ == "__main__":
    unittest.main()
