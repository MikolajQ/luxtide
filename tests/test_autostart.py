"""Start at login: the XDG entry as a switch, with and without a system entry."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401
from luxtide import autostart

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"


class Switch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.home = root / "config"
        self.system = root / "xdg"
        env = {"XDG_CONFIG_HOME": str(self.home), "XDG_CONFIG_DIRS": str(self.system)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Never touch the real user manager from a test.
        patcher = mock.patch.object(autostart.shutil, "which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def install_system_entry(self):
        directory = self.system / "autostart"
        directory.mkdir(parents=True)
        text = (DATA / autostart.ENTRY_NAME).read_text().replace("@BINDIR@", "/usr/bin")
        (directory / autostart.ENTRY_NAME).write_text(text)

    def test_user_entry_alone(self):
        self.assertFalse(autostart.is_enabled())
        autostart.set_enabled(True, exec_path="/home/u/.local/bin/luxtide")
        entry = autostart.read_entry(autostart.user_entry())
        self.assertEqual(entry["Exec"], "/home/u/.local/bin/luxtide autostart")
        self.assertTrue(autostart.is_enabled())
        autostart.set_enabled(False)
        self.assertFalse(autostart.user_entry().exists())
        self.assertFalse(autostart.is_enabled())

    def test_package_entry_is_on_by_default_and_overridden_per_user(self):
        self.install_system_entry()
        self.assertTrue(autostart.is_enabled())
        autostart.set_enabled(False)
        entry = autostart.read_entry(autostart.user_entry())
        self.assertEqual(entry["Hidden"], "true")
        self.assertFalse(autostart.is_enabled())
        autostart.set_enabled(True)
        self.assertFalse(autostart.user_entry().exists())
        self.assertTrue(autostart.is_enabled())

    def test_desktop_settings_switch_is_understood(self):
        # GNOME Tweaks and others write X-GNOME-Autostart-enabled=false.
        self.install_system_entry()
        user = autostart.user_entry()
        user.parent.mkdir(parents=True)
        user.write_text(autostart.entry_text(True).replace(
            "X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-enabled=false"))
        self.assertFalse(autostart.is_enabled())

    def test_off_also_disables_the_systemd_unit(self):
        wants = self.home / "systemd" / "user" / "graphical-session.target.wants"
        wants.mkdir(parents=True)
        (wants / autostart.UNIT).symlink_to("/nonexistent")
        self.assertTrue(autostart.is_enabled())
        autostart.set_enabled(False)
        self.assertFalse(autostart.unit_links())
        self.assertFalse(autostart.is_enabled())

    def systemctl(self):
        """Pretend a user manager knows the unit; record what we ask it."""
        calls = []

        def run(argv, **kw):
            calls.append(argv[2:])
            return mock.Mock(returncode=0, stdout="", stderr="")
        for name, value in (("systemd_unit_available", lambda: True),):
            patcher = mock.patch.object(autostart, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(autostart.subprocess, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_on_enables_the_unit_for_early_start(self):
        calls = self.systemctl()
        autostart.set_enabled(True, exec_path="/usr/bin/luxtide")
        self.assertEqual(calls, [["enable", autostart.UNIT]])

    def test_login_entry_moves_a_unit_enabled_by_an_older_version(self):
        calls = self.systemctl()
        self.install_system_entry()
        wants = self.home / "systemd" / "user" / "graphical-session.target.wants"
        wants.mkdir(parents=True)
        (wants / autostart.UNIT).symlink_to("/nonexistent")
        self.assertTrue(autostart.adopt_unit())
        self.assertEqual(calls, [["reenable", autostart.UNIT]])

    def test_login_entry_removes_the_dangling_unit_of_the_old_package(self):
        wants = self.home / "systemd" / "user" / "graphical-session.target.wants"
        wants.mkdir(parents=True)
        (wants / autostart.LEGACY_UNIT).symlink_to("/usr/lib/systemd/user/gone.service")
        (wants / "other.service").symlink_to("/nonexistent")
        autostart.adopt_unit()
        self.assertFalse((wants / autostart.LEGACY_UNIT).is_symlink())
        self.assertTrue((wants / "other.service").is_symlink())

    def test_login_entry_leaves_an_early_unit_alone(self):
        calls = self.systemctl()
        self.install_system_entry()
        wants = self.home / "systemd" / "user" / f"{autostart.EARLY_TARGET}.wants"
        wants.mkdir(parents=True)
        (wants / autostart.UNIT).symlink_to("/nonexistent")
        self.assertFalse(autostart.adopt_unit())
        self.assertEqual(calls, [])

    def test_paths_with_spaces_are_quoted(self):
        text = autostart.entry_text(True, exec_path="/opt/my apps/luxtide")
        self.assertIn('Exec="/opt/my apps/luxtide" autostart', text)

    def test_packaged_entry_matches_the_one_we_write(self):
        packaged = autostart.read_entry(DATA / autostart.ENTRY_NAME)
        path = pathlib.Path(self.tmp.name) / "written.desktop"
        path.write_text(autostart.entry_text(True, exec_path="@BINDIR@/luxtide"))
        self.assertEqual(packaged, autostart.read_entry(path))


if __name__ == "__main__":
    unittest.main()
