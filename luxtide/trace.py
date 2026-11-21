"""TEMPORARY: a detailed trace of the brightness keys on laptops.

Written to ~/luxtide-trace-HOST.txt by the daemon of a machine with a
backlight, to find out why the keys misbehave near the bottom of the range.
It gets:

- every log record of the daemon, debug ones included, with millisecond
  timestamps (the journal keeps getting the usual info lines only);
- the panel's raw brightness from sysfs, every 5 ms for 3 s after anything
  happens (a key, a write) and every 200 ms otherwise, changes only;
- GNOME Shell's saved scale whenever the shell saves it, which it does
  300 ms after every change by the user: the shell's real scale, to set
  against the daemon's idea of it.

To be removed with every call to it once the problem is understood.
"""

import logging
import os
import re
import socket
import threading
import time

log = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024
FAST, SLOW, FAST_FOR = 0.005, 0.2, 3.0
_LOCATION = re.compile(r"location -?[\d.]+, -?[\d.]+")

_lock = threading.Lock()
_handle = None
_sampler = None


def path():
    return os.path.join(os.path.expanduser("~"), f"luxtide-trace-{socket.gethostname()}.txt")


def active():
    return _handle is not None


def line(text):
    """One line with wall-clock and monotonic time."""
    global _handle
    if _handle is None:
        return
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
    with _lock:
        if _handle is None:
            return
        try:
            _handle.write(f"{stamp} {time.monotonic():10.3f} {text}\n")
            if _handle.tell() > MAX_BYTES:
                _handle.write("trace full, stopped\n")
                _handle.close()
                _handle = None
        except (OSError, ValueError):
            _handle = None


def poke():
    """Something happened: sample the panel densely for a while."""
    if _sampler is not None:
        _sampler.poke()


class _Handler(logging.Handler):
    def emit(self, record):
        try:
            text = _LOCATION.sub("location <masked>", record.getMessage())
            line(f"[{record.levelname.lower()} {record.name}] {text}")
        except Exception:
            pass


class _Sampler(threading.Thread):
    def __init__(self, brightness):
        super().__init__(name="trace-sampler", daemon=True)
        self.brightness = brightness
        self.fast_until = 0.0
        self.wake = threading.Event()

    def poke(self):
        self.fast_until = time.monotonic() + FAST_FOR
        self.wake.set()

    def run(self):
        last = None
        while _handle is not None:
            try:
                with open(self.brightness, encoding="ascii") as handle:
                    value = handle.read().strip()
            except OSError:
                value = "?"
            if value != last:
                line(f"sysfs {value}")
                last = value
            fast = time.monotonic() < self.fast_until
            self.wake.wait(FAST if fast else SLOW)
            self.wake.clear()


def start(backlight_dir, verbose=False):
    """Open the trace file and start sampling `backlight_dir`. Quiet on failure."""
    global _handle, _sampler
    try:
        _handle = open(path(), "a", encoding="utf-8", buffering=1)
    except OSError:
        return False
    # Debug records go to the trace; the journal keeps getting what it got.
    for handler in logging.getLogger().handlers:
        if handler.level == logging.NOTSET and not verbose:
            handler.setLevel(logging.INFO)
    root = logging.getLogger("luxtide")
    root.setLevel(logging.DEBUG)
    root.addHandler(_Handler())
    line("=" * 70)
    line(f"trace started: {time.strftime('%Y-%m-%d %H:%M:%S')}, backlight {backlight_dir}, "
         f"desktop {os.environ.get('XDG_CURRENT_DESKTOP', '?')}")
    brightness = os.path.join(backlight_dir, "brightness")
    for name in ("max_brightness", "brightness", "actual_brightness", "type"):
        try:
            with open(os.path.join(backlight_dir, name), encoding="ascii") as handle:
                line(f"  {name} = {handle.read().strip()}")
        except OSError:
            pass
    _sampler = _Sampler(brightness)
    _sampler.start()
    return True


def watch_shell_state(state_path, reader):
    """Log GNOME Shell's saved brightness each time the shell writes it.
    `reader(path)` returns the entries or None. Needs a GLib main loop."""
    if _handle is None:
        return None
    from gi.repository import Gio

    def dump(reason):
        entries = reader(state_path)
        if entries is None:
            line(f"shell-saved ({reason}): unreadable or missing")
            return
        text = ", ".join(f"{e[4]}={e[5]:.4f}" for e in entries)
        line(f"shell-saved ({reason}): {text}")

    def changed(monitor, file, other, event):
        if event in (Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED,
                     Gio.FileMonitorEvent.RENAMED, Gio.FileMonitorEvent.MOVED_IN):
            dump("changed")
    try:
        dump("start")
        monitor = Gio.File.new_for_path(str(state_path)).monitor_file(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        monitor.connect("changed", changed)
        return monitor
    except Exception as error:
        line(f"shell-saved: cannot watch: {error}")
        return None
