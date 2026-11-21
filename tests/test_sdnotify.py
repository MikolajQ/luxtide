"""Readiness for systemd's Type=notify."""

import os
import socket
import tempfile
import unittest
from unittest import mock

from luxtide import sdnotify


class Notify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "notify")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.settimeout(1)
        self.addCleanup(self.sock.close)
        patcher = mock.patch.dict(os.environ, {"NOTIFY_SOCKET": self.path})
        patcher.start()
        self.addCleanup(patcher.stop)
        sdnotify._sent = False
        self.addCleanup(setattr, sdnotify, "_sent", False)

    def backlights(self, *names):
        root = os.path.join(self.tmp.name, "backlight")
        os.makedirs(root, exist_ok=True)
        for name in names:
            os.makedirs(os.path.join(root, name))
        return root

    def test_ready_once(self):
        self.assertTrue(sdnotify.ready())
        self.assertEqual(self.sock.recv(64), b"READY=1")
        self.assertFalse(sdnotify.ready())

    def test_no_panel_is_ready_at_once(self):
        self.assertTrue(sdnotify.early_ready(self.backlights()))
        self.assertEqual(self.sock.recv(64), b"READY=1")

    def test_a_laptop_lets_the_daemon_say_it(self):
        root = self.backlights("amdgpu_bl1")
        self.assertFalse(sdnotify.early_ready(root))
        self.assertTrue(sdnotify.ready())

    def test_missing_directory_counts_as_no_panel(self):
        self.assertTrue(sdnotify.early_ready(os.path.join(self.tmp.name, "none")))

    def test_outside_systemd_nothing_is_sent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(sdnotify.notify("READY=1"))
