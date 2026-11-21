"""Display discovery and actuators against fake sysfs trees and fake buses."""

import os
import pathlib
import shutil
import tempfile
import unittest
import weakref

from helpers import edid
from luxtide import displays
from luxtide.backends import backlight
from luxtide.config import DdcSettings


class FakeSysfs:
    def __init__(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        weakref.finalize(self, shutil.rmtree, self.root, True)
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
        self.handlers = {}

    def subscribe(self, sender, interface, member, path, handler):
        self.handlers[(path, member)] = handler
        return lambda: self.handlers.pop((path, member), None)

    def emit(self, path, member, *args):
        self.handlers[(path, member)](args)

    def call(self, name, path, interface, method, signature=None, args=(), reply=None):
        if method in self.fail:
            raise RuntimeError(f"{method} refused")
        if method == "GetAll":
            return (self.properties[(path, args[0])],)
        if method == "GetConnectionUnixProcessID":
            return (getattr(self, "pids", {})[args[0]],)
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

    def test_mutter_waits_for_gnome_shell(self):
        proc = self.fs.root / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "comm").write_text("gnome-shell\n")
        session = self.mutter_bus()
        session.pids = {backlight.MutterMethod.BUS: 4242}
        system = FakeBus({}, owners={"org.freedesktop.login1"})
        actuator = backlight.BacklightActuator(self.device, session_bus=session,
                                               system_bus=system)
        original = backlight.MutterMethod.PROC
        backlight.MutterMethod.PROC = str(proc)
        self.addCleanup(setattr, backlight.MutterMethod, "PROC", original)
        self.assertEqual(actuator.select().name, "logind")
        (proc / "4242" / "comm").write_text("gnome-kiosk\n")
        self.assertEqual(actuator.select().name, "mutter")

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


class FakeScheduler:
    def __init__(self):
        self.pending = {}
        self.next = 0

    def timeout(self, seconds, callback):
        self.next += 1
        self.pending[self.next] = callback
        return self.next

    def cancel(self, handle):
        self.pending.pop(handle, None)

    def run(self):
        while self.pending:
            self.step()

    def step(self):
        self.pending.pop(min(self.pending))()


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ShellModelTest(unittest.TestCase):
    EPS = 0.0005

    def test_target_cancels_the_scale(self):
        model = backlight.ShellModel(0.4)
        model.target = model.target_for(0.89)
        self.assertAlmostEqual(model.panel(), 0.89)
        self.assertAlmostEqual(model.target, 0.99)

    def test_a_high_scale_sets_a_floor(self):
        model = backlight.ShellModel(1.0)
        self.assertEqual(model.target_for(0.01), 0.0)
        model.target = 0.0
        self.assertAlmostEqual(model.panel(), 0.5)
        self.assertAlmostEqual(model.lowest(), 0.5)

    def test_key_press_moves_the_scale_one_step(self):
        model = backlight.ShellModel(0.4)
        model.target = model.target_for(0.5)
        self.assertEqual(model.user_change(0.5, 0.45, 1, self.EPS), (-1, True))
        self.assertAlmostEqual(model.scale, 0.35)

    def test_held_key_counts_presses(self):
        model = backlight.ShellModel(0.5)
        model.target = model.target_for(0.5)
        self.assertEqual(model.user_change(0.5, 0.65, 3, self.EPS), (1, True))
        self.assertAlmostEqual(model.scale, 0.65)

    def test_slider_is_taken_as_it_is(self):
        model = backlight.ShellModel(0.5)
        model.target = model.target_for(0.6)
        self.assertEqual(model.user_change(0.6, 0.23, 4, self.EPS), (-1, False))
        self.assertAlmostEqual(model.scale, 0.13)

    def test_up_at_full_brightness_changes_nothing_visible(self):
        model = backlight.ShellModel(0.5)
        model.target = model.target_for(1.0)
        self.assertEqual(model.user_change(1.0, 1.0, 1, self.EPS), (1, True))
        self.assertAlmostEqual(model.scale, 0.55)

    def test_down_at_the_bottom_of_the_scale(self):
        model = backlight.ShellModel(0.0)
        model.target = model.target_for(0.3)
        self.assertEqual(model.user_change(0.3, 0.3, 1, self.EPS), (-1, True))
        self.assertAlmostEqual(model.scale, 0.0)

    def test_down_below_the_panel_minimum(self):
        model = backlight.ShellModel(0.5)
        model.target = model.target_for(0.01)
        self.assertEqual(model.user_change(0.01, 0.0, 1, self.EPS), (-1, True))
        self.assertAlmostEqual(model.scale, 0.45)

    def test_learn_corrects_the_scale(self):
        model = backlight.ShellModel(0.5)
        model.target = model.target_for(0.6)
        self.assertTrue(model.learn(0.75, self.EPS))
        self.assertAlmostEqual(model.scale, 0.65)
        self.assertFalse(model.learn(0.75, self.EPS))
        self.assertFalse(model.learn(1.0, self.EPS))      # clamped: says nothing


class GnomeShell(unittest.TestCase):
    """org.gnome.Shell.Brightness on an amdgpu panel whose range starts at
    4000 of 400000, as on the laptop this was written for."""

    def setUp(self):
        fs = self.fs = FakeSysfs()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = fs.device("amdgpu_bl1", "raw", 400000, 166000)      # scale 0.4091
        connector = displays.Connector("card1-eDP-1", fs.connector("card1-eDP-1"), True, None)
        self.device = displays.BacklightDevice("amdgpu_bl1", self.path, "raw", 400000,
                                               connector)
        entries = [{"connector": "eDP-1", "active": True, "min": 4000, "max": 400000,
                    "value": 166000}]
        Shell = backlight.GnomeShellMethod
        self.bus = FakeBus({(backlight.MutterMethod.PATH, "Backlight"): (2, entries),
                            (Shell.PATH, "HasBrightnessControl"): True})
        self.store = backlight._ScaleStore(pathlib.Path(self.tmp.name) / "shell.json")
        self.actuator = backlight.BacklightActuator(
            self.device, session_bus=self.bus, system_bus=FakeBus({}), store=self.store)
        self.clock = FakeClock()

    def shell_writes(self, relative):
        """What the shell does with the target: set the panel."""
        raw = round(4000 + 396000 * relative)
        (self.path / "brightness").write_text(f"{raw}\n")

    def last_target(self):
        name, path, method, args = self.bus.calls[-1]
        self.assertEqual(method, "SetAutoBrightnessTarget")
        return args[0]

    def test_target_is_compensated_and_checked(self):
        self.assertTrue(self.actuator.write(89))
        self.assertEqual(self.actuator.method.name, "gnome-shell")
        self.assertTrue(self.actuator.events)
        target = self.last_target()
        scale = (166000 - 4000) / 396000
        self.assertAlmostEqual(target, 0.89 - (scale - 0.5), places=6)
        self.shell_writes(0.89)
        self.assertFalse(self.actuator.check())            # as predicted
        self.assertAlmostEqual(self.store.load("amdgpu_bl1"), scale, places=5)
        self.assertEqual(self.actuator.read(), 89)

    def test_wrong_guess_is_corrected(self):
        self.store.save("amdgpu_bl1", 0.5)
        self.actuator.write(60)
        self.assertAlmostEqual(self.last_target(), 0.6)
        self.shell_writes(0.75)                            # the scale was 0.65
        self.assertTrue(self.actuator.check())
        self.assertAlmostEqual(self.store.load("amdgpu_bl1"), 0.65, places=5)
        self.actuator.write(60)
        self.assertAlmostEqual(self.last_target(), 0.45)

    def test_key_press_is_reported_once_settled(self):
        changes = []
        scheduler = FakeScheduler()
        self.store.save("amdgpu_bl1", 0.5)
        self.actuator.write(50)
        self.actuator.watch(scheduler, changes.append)
        self.shell_writes(0.45)
        for _ in range(2):                                  # a held key: two presses
            self.bus.emit(backlight.GnomeShellMethod.PATH, "BrightnessChanged")
        self.shell_writes(0.40)
        scheduler.run()
        self.assertEqual(changes, [backlight.UserChange(40, -1, 2, True)])
        self.assertAlmostEqual(self.store.load("amdgpu_bl1"), 0.4, places=5)
        self.actuator.write(41)
        self.assertAlmostEqual(self.last_target(), 0.51)

    def watched(self, scale, level):
        changes = []
        scheduler = FakeScheduler()
        self.store.save("amdgpu_bl1", scale)
        self.actuator.write(level)
        self.actuator.method.clock = self.clock
        self.actuator.watch(scheduler, changes.append)
        return changes, scheduler

    def test_key_press_counts_the_moment_it_lands(self):
        changes, scheduler = self.watched(0.5, 50)
        self.shell_writes(0.50)
        self.bus.emit(backlight.GnomeShellMethod.PATH, "BrightnessChanged")
        scheduler.step()                    # not in sysfs yet
        self.assertEqual(changes, [])
        self.clock.now += 0.015
        self.shell_writes(0.55)
        scheduler.step()
        self.assertEqual(changes, [backlight.UserChange(55, 1, 1, True)])
        self.assertEqual(scheduler.pending, {})

    def test_slider_waits_until_it_stops(self):
        changes, scheduler = self.watched(0.5, 50)
        for panel in (0.47, 0.37, 0.33):
            self.shell_writes(panel)
            self.bus.emit(backlight.GnomeShellMethod.PATH, "BrightnessChanged")
            scheduler.step()
            self.clock.now += 0.1
        self.assertEqual(changes, [])
        self.clock.now += 0.31
        scheduler.run()
        self.assertEqual(changes, [backlight.UserChange(33, -1, 3, False)])
        self.assertAlmostEqual(self.store.load("amdgpu_bl1"), 0.33, places=5)

    def test_press_without_visible_change_waits_for_quiet(self):
        changes, scheduler = self.watched(0.5, 100)
        self.shell_writes(1.0)
        self.bus.emit(backlight.GnomeShellMethod.PATH, "BrightnessChanged")
        scheduler.step()
        self.assertEqual(changes, [])
        self.clock.now += 0.31
        scheduler.run()
        self.assertEqual(changes, [backlight.UserChange(100, 1, 1, True)])

    def test_signals_before_our_first_target_are_not_reported(self):
        changes = []
        self.actuator.select()
        self.actuator.watch(FakeScheduler(), changes.append)
        self.bus.emit(backlight.GnomeShellMethod.PATH, "BrightnessChanged")
        self.assertEqual(changes, [])

    def test_leaving_the_shell_hands_the_panel_back(self):
        self.actuator.write(50)
        self.actuator.requested = "sysfs"
        os.chmod(self.path / "brightness", 0o644)
        self.actuator.select()
        self.assertEqual(self.actuator.method.name, "sysfs")
        self.assertEqual(self.last_target(), -1.0)

    def test_floor_is_reported(self):
        self.store.save("amdgpu_bl1", 0.9)
        self.actuator.write(10)
        self.assertEqual(self.last_target(), 0.0)
        self.assertIn("40 %", self.actuator.note)


class KdeKeys(unittest.TestCase):
    def test_key_press_is_reported(self):
        fs = self.fs = FakeSysfs()
        path = fs.device("amdgpu_bl1", "raw", 65535, 50000)
        device = displays.BacklightDevice("amdgpu_bl1", path, "raw", 65535, None)
        display = "/org/kde/ScreenBrightness/display0"
        bus = FakeBus({
            (backlight.MutterMethod.PATH, "Backlight"): (1, []),
            (backlight.KdeMethod.PATH, "DisplaysDBusNames"): ["display0"],
            (display, backlight.KdeMethod.DISPLAY):
                {"IsInternal": True, "MaxBrightness": 10000, "Brightness": 8500},
        }, owners={"org.kde.ScreenBrightness"})
        actuator = backlight.BacklightActuator(device, session_bus=bus, system_bus=FakeBus({}))
        changes = []
        actuator.watch(FakeScheduler(), changes.append)
        actuator.write(85)
        self.assertEqual(actuator.method.name, "kde")
        kde = backlight.KdeMethod.PATH
        bus.emit(kde, "BrightnessChanged", "display0", 9000, "(internal)", "brightness_key")
        bus.emit(kde, "BrightnessChanged", "display0", 9000, "(internal)", "dim_display")
        bus.emit(kde, "BrightnessChanged", "display0", 8500, "(internal)", "brightness_key")
        self.assertEqual(changes, [backlight.UserChange(90, 1, 1, True),
                                   backlight.UserChange(85, -1, 1, True)])


class Steps(unittest.TestCase):
    def test_relative_steps(self):
        from luxtide.policy import step_level
        self.assertEqual(step_level(50, 1, 10), 55)
        self.assertEqual(step_level(55, -1, 10), 50)
        self.assertEqual(step_level(1, 1, 10), 2)
        self.assertEqual(step_level(2, -1, 10), 1)
        self.assertEqual(step_level(1, -1, 10), 1)
        self.assertEqual(step_level(95, 1, 10), 100)
        self.assertEqual(step_level(100, 1, 10), 100)
        self.assertEqual(step_level(50, -1, 10, presses=3), 37)


class DdcCommand(unittest.TestCase):
    def test_flags(self):
        from luxtide.backends import ddc
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
        from luxtide.backends import ddc
        actuator = ddc.DdcActuator(5, DdcSettings(repeats=3, settle=0), dry_run=True)
        self.assertEqual(actuator.set_vcp(ddc.VCP_CONTRAST, 60), (3, 3))


if __name__ == "__main__":
    unittest.main()
