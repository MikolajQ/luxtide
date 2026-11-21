"""GNOME Shell's saved brightness, preset before the shell starts."""

import os
import pathlib
import tempfile
import unittest

import helpers  # noqa: F401
from gi.repository import GLib

from luxtide.backends import shell_state

PANEL = ("BOE", "0x0b0e", "0x00000000", "default", "eDP-1", 0.55)
EXTERNAL = ("DEL", "0xa1b2", "ABC123", "default", "DP-1", 0.8)


class Preset(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.path = self.root / "brightnesses"
        self.proc = self.root / "proc"
        self.proc.mkdir()

    def save(self, *entries):
        data = GLib.Variant(shell_state.STATE_TYPE, list(entries)).get_data_as_bytes()
        self.path.write_bytes(data.get_data())
        os.chmod(self.path, 0o644)

    def process(self, pid, comm):
        directory = self.proc / str(pid)
        directory.mkdir()
        (directory / "comm").write_text(comm + "\n")

    def preset(self, level, connector="eDP-1"):
        return shell_state.preset(connector, level, self.path, proc=self.proc)

    def test_panel_entry_gets_our_level(self):
        self.save(PANEL, EXTERNAL)
        self.process(100, "gnome-session-s")
        self.assertAlmostEqual(self.preset(15), 0.15)
        entries = shell_state.read(self.path)
        self.assertEqual(entries[0][:5], PANEL[:5])
        self.assertAlmostEqual(entries[0][5], 0.15)
        self.assertEqual(tuple(entries[1]), EXTERNAL)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
        self.assertEqual(list(self.root.glob(".*tmp")), [])

    def test_bright_levels_start_at_half(self):
        self.save(PANEL)
        self.assertAlmostEqual(self.preset(100), 0.5)
        self.assertEqual(shell_state.start_scale(40), 0.4)

    def test_never_behind_a_running_shell(self):
        self.save(PANEL)
        before = self.path.read_bytes()
        self.process(200, "gnome-shell")
        self.assertIsNone(self.preset(15))
        self.assertEqual(self.path.read_bytes(), before)

    def test_no_entry_for_the_panel(self):
        self.save(EXTERNAL)
        before = self.path.read_bytes()
        self.assertIsNone(self.preset(15))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_or_foreign_file_is_left_alone(self):
        self.assertIsNone(self.preset(15))
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b"not a variant at all")
        self.assertIsNone(self.preset(15))
        self.assertEqual(self.path.read_bytes(), b"not a variant at all")

    def test_same_value_is_not_rewritten(self):
        self.save(PANEL[:5] + (0.15,))
        mtime = os.stat(self.path).st_mtime_ns
        self.assertAlmostEqual(self.preset(15), 0.15)
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime)


if __name__ == "__main__":
    unittest.main()
