"""The real daemon against fake_shell.py, on a private session bus.

Checks what unit tests cannot: name watching, signal delivery and timing
through a real bus. The shell appears after the daemon (as at login with
early start) and starts from the brightness the daemon saved for it, two
kinds of key presses, then the shell restarts.

    make test-shell
    dbus-run-session -- python3 tools/shell-test/run.py . [INITIAL_SCALE]

The system bus is the real one: logind is asked to write a backlight that
does not exist there and refuses, which is expected.
"""
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

repo = pathlib.Path(sys.argv[1])
here = pathlib.Path(__file__).parent
tmp = pathlib.Path(tempfile.mkdtemp())
for name in ("config", "state", "cache", "run", "data"):
    (tmp / name).mkdir()
os.environ.update(XDG_CONFIG_HOME=str(tmp / "config"), XDG_STATE_HOME=str(tmp / "state"),
                  XDG_CACHE_HOME=str(tmp / "cache"), XDG_RUNTIME_DIR=str(tmp / "run"),
                  XDG_DATA_HOME=str(tmp / "data"))
(tmp / "config" / "luxtide").mkdir()
(tmp / "config" / "luxtide" / "config.ini").write_text(
    "[location]\nlatitude = 50.0614\nlongitude = 19.9366\n"
    "[levels]\nday = 30\ntwilight = 30\nnight = 30\n"
    "[ddc]\nenabled = no\n[tray]\nenabled = no\n")
drm = tmp / "drm" / "card1-eDP-1"
drm.mkdir(parents=True)
(drm / "status").write_text("connected\n")
(drm / "dpms").write_text("On\n")
(drm / "edid").write_bytes(b"")
bl = tmp / "backlight" / "amdgpu_bl1"
bl.mkdir(parents=True)
(bl / "type").write_text("raw\n")
(bl / "max_brightness").write_text("400000\n")
(bl / "brightness").write_text("356440\n")
brightness = bl / "brightness"

sys.path.insert(0, str(repo))
from gi.repository import Gio, GLib  # noqa: E402
from luxtide import displays  # noqa: E402
from luxtide.backends import shell_state  # noqa: E402
from luxtide.daemon import Daemon  # noqa: E402

initial = float(sys.argv[2]) if len(sys.argv) > 2 else 0.7
# What the shell saved last session; the daemon presets it before the shell
# starts. On a machine running GNOME the real shell would count as already
# running, so the check is replaced: here the daemon comes first, as at login.
saved = tmp / "data" / "gnome-shell" / "brightnesses"
saved.parent.mkdir()
saved.write_bytes(GLib.Variant("a(sssssd)", [
    ("BOE", "0x0b0e", "0x00000000", "default", "eDP-1", initial),
    ("DEL", "0xa1b2", "ABC123", "default", "DP-1", 0.8)]).get_data_as_bytes().get_data())
shell_state.shell_running = lambda proc="/proc", uid=None: False

orig_c, orig_b = displays.list_connectors, displays.list_backlights
displays.list_connectors = lambda root=tmp / "drm": orig_c(root)
displays.list_backlights = lambda root=tmp / "backlight", connectors=None: orig_b(root, connectors)
logging.basicConfig(level=logging.INFO, format="%(relativeCreated)6.0f %(name)s: %(message)s")

daemon = Daemon(dry_run=False, tray=False)
shell = {"proc": None}
failures = []


def panel():
    return (int(brightness.read_text()) - 4000) / 396000


def start_shell(scale):
    shell["proc"] = subprocess.Popen([sys.executable, str(here / "fake_shell.py"),
                                      str(brightness), str(scale)])


def press(direction):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    bus.call_sync("test.Shell", "/test", "test.Control", "Press",
                  GLib.Variant("(i)", (direction,)), None, Gio.DBusCallFlags.NONE, 2000, None)


def expect(what, value, wanted):
    ok = abs(value - wanted) < 0.006
    print(f"CHECK {'ok  ' if ok else 'FAIL'} {what}: {value:.3f} (wanted {wanted:.3f})", flush=True)
    if not ok:
        failures.append(what)


def manual():
    return next(iter(daemon.status()["outputs"]))["manual"]


def start_panel():
    return float((bl / "brightness.start").read_text())


steps = [
    (1.0, lambda: start_shell("saved")),
    (2.0, lambda: expect("the shell started at our level, no flash", start_panel(), 0.30)),
    (3.0, lambda: expect("level after the shell appeared", panel(), 0.30)),
    (3.5, lambda: press(-1)),
    (3.6, lambda: expect("one press down, 0.1 s later: our step 30 -> 25", panel(), 0.25)),
    (4.5, lambda: expect("one press down, settled", panel(), 0.25)),
    (4.6, lambda: expect("held level", manual(), 25)),
    (4.6, lambda: expect("status: level on screen", daemon.status()["shown"], 25)),
    (4.6, lambda: expect("status: level from the keys", daemon.status()["keys_level"], 25)),
    (4.6, lambda: expect("status: schedule unchanged", daemon.status()["level"], 30)),
    (5.0, lambda: (press(1), press(1))),
    (6.0, lambda: expect("two presses up: 25 -> 30 -> 36", panel(), 0.36)),
    (6.5, lambda: shell["proc"].terminate()),
    (7.5, lambda: start_shell(initial + 0.05)),   # restarted: its saved scale
    (9.5, lambda: expect("after a shell restart", panel(), 0.36)),
    (10.0, daemon.quit),
]
for at, step in steps:
    GLib.timeout_add(int(at * 1000), lambda s=step: (s(), False)[1])
code = daemon.run()
if shell["proc"]:
    shell["proc"].terminate()
print("store:", (tmp / "state" / "luxtide" / "gnome-shell.json").read_text()
      if (tmp / "state" / "luxtide" / "gnome-shell.json").exists() else None)
print("RESULT", "FAILED " + ", ".join(failures) if failures else "all ok")
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if failures or code else 0)
