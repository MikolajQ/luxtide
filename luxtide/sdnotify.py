"""Readiness for systemd (Type=notify), without libsystemd.

Enabled, the unit is wanted by graphical-session-pre.target, and a target
waits for the units it wants: the desktop starts only once we say we are
ready. That wait is worth it on a laptop, where the desktop restores the
brightness of the last session as it comes up and the sooner we are there,
the shorter that shows. Anywhere else nothing is gained, so the desktop is
let go at once, before the daemon has even loaded.

Imports little on purpose: early_ready() runs before anything else.
"""

import os
import socket

BACKLIGHT_ROOT = "/sys/class/backlight"

_sent = False


def notify(message):
    """Send one sd_notify message; False when not run by systemd."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]                  # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(path)
            sock.sendall(message.encode())
    except OSError:
        return False
    return True


def ready():
    """Tell systemd we are up; only the first call sends anything."""
    global _sent
    if _sent:
        return False
    _sent = True
    return notify("READY=1")


def has_backlight(root=BACKLIGHT_ROOT):
    try:
        return any(True for _ in os.scandir(root))
    except OSError:
        return False


def early_ready(root=BACKLIGHT_ROOT):
    """Without a laptop panel, ready right away; with one, ready() comes
    from the daemon once it is on the bus. True when sent here."""
    if has_backlight(root):
        return False
    return ready()
