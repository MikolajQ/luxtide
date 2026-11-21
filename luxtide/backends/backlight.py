"""Built-in laptop panels.

Writing /sys/class/backlight directly works, but on a modern desktop it goes
behind the back of whoever owns the brightness: the desktop's slider stops
matching the screen, and the desktop restores its own value after idle
dimming or a resume. So the desktop's own interface comes first:

    gnome-shell  GNOME 49 and later: the shell's automatic brightness target
                 (org.gnome.Shell.Brightness), see ShellModel
    mutter       GNOME 49 and later without that interface:
                 org.gnome.Mutter.DisplayConfig.SetBacklight
    kde          Plasma 6: org.kde.ScreenBrightness
    logind       everything else (sway, Hyprland, niri, XFCE...):
                 Session.SetBrightness, no root needed
    sysfs        last resort, needs write permission on the brightness file

Reading always works, which lets us notice the brightness keys. GNOME Shell
and Plasma also announce the keys on the bus, so there they are handled the
moment they are pressed, with Luxtide's own step instead of the desktop's.
"""

import dataclasses
import json
import logging
import os
import threading
import time

from .. import clamp_level, paths, trace

log = logging.getLogger(__name__)

TIMEOUT_MS = 3000
SHELL_STEPS = 20          # brightnessManager.js: SCALE_VALUE_N_STEPS


@dataclasses.dataclass
class UserChange:
    """The user changed the brightness through the desktop."""
    level: int            # what the panel shows now, in percent
    direction: int        # +1 brighter, -1 dimmer, 0 unknown
    presses: int          # key presses counted, for a held key
    keys: bool            # True: brightness keys; False: a slider


def percent_to_raw(level, maximum, minimum=0):
    """Map 1-100 % onto a device range, never landing on 'off'."""
    raw = minimum + round((maximum - minimum) * level / 100.0)
    return max(raw, minimum if minimum > 0 else 1)


def raw_to_percent(raw, maximum, minimum=0):
    if maximum <= minimum:
        return 100
    return clamp_level((raw - minimum) * 100.0 / (maximum - minimum))


class _Bus:
    """Thin synchronous D-Bus caller, replaceable in tests."""

    def __init__(self, system=False):
        from gi.repository import Gio
        self.Gio = Gio
        kind = Gio.BusType.SYSTEM if system else Gio.BusType.SESSION
        self.connection = Gio.bus_get_sync(kind, None)

    def call(self, name, path, interface, method, signature=None, args=(), reply=None):
        from gi.repository import GLib
        params = GLib.Variant(signature, args) if signature else None
        result = self.connection.call_sync(
            name, path, interface, method, params,
            GLib.VariantType(reply) if reply else None,
            self.Gio.DBusCallFlags.NO_AUTO_START, TIMEOUT_MS, None)
        return result.unpack() if result is not None else ()

    def get(self, name, path, interface, prop):
        return self.call(name, path, "org.freedesktop.DBus.Properties", "Get",
                         "(ss)", (interface, prop), "(v)")[0]

    def has_owner(self, name):
        try:
            return self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                             "org.freedesktop.DBus", "NameHasOwner", "(s)", (name,),
                             "(b)")[0]
        except Exception:
            return False

    def subscribe(self, sender, interface, member, path, handler):
        """Call handler(args) for each matching signal, in the main loop.
        Returns a function that cancels the subscription."""
        def deliver(connection, sender, path, interface, signal, params):
            try:
                handler(params.unpack())
            except Exception:
                log.exception("handling %s.%s failed", interface, signal)
        handle = self.connection.signal_subscribe(
            sender, interface, member, path, None, self.Gio.DBusSignalFlags.NONE, deliver)
        return lambda: self.connection.signal_unsubscribe(handle)


def pick_mutter_entry(entries, device):
    """The entry of Mutter's Backlight property that belongs to `device`."""
    usable = [e for e in entries if "max" in e and "connector" in e]
    wanted = device.connector.short if device.connector else None
    for entry in usable:
        if entry["connector"] == wanted:
            return entry
    for entry in usable:
        if entry["connector"].startswith(("eDP", "LVDS", "DSI")):
            return entry
    return usable[0] if usable else None


class ShellModel:
    """How GNOME Shell sets the backlight (misc/brightnessManager.js, GNOME 49+).

    The shell keeps a scale per monitor, 0-1, which the brightness keys move
    in steps of 1/20 and the quick settings slider moves freely. It remembers
    the scale across sessions and, at login, sets the backlight to it. Once
    an automatic target is set (SetAutoBrightnessTarget, meant for a light
    sensor), the panel shows

        clamp(target + scale - 0.5, 0, 1)

    so the keys and the slider become an offset around the automatic level.
    Luxtide sends a target that cancels the offset, so the panel shows what
    the schedule asks for. Writing the backlight past the shell (Mutter's
    SetBacklight, sysfs) would update the scale of the monitor but not the
    global scale the keys move, and the next key press would jump from a
    stale value: 94 % to 45 % on one laptop.

    A negative target switches the automatic mode off, so the offset can be
    cancelled only down to scale - 0.5; with the scale above 0.5 the panel
    cannot go lower until the user presses brightness down.
    """

    def __init__(self, scale, steps=SHELL_STEPS):
        self.scale = min(1.0, max(0.0, scale))
        self.steps = max(1, steps)
        self.target = None

    @property
    def step(self):
        return 1.0 / self.steps

    def panel(self, target=None, scale=None):
        target = self.target if target is None else target
        scale = self.scale if scale is None else scale
        if target is None or target < 0:
            return scale
        return min(1.0, max(0.0, target + scale - 0.5))

    def target_for(self, wanted):
        return max(0.0, wanted - (self.scale - 0.5))

    def lowest(self):
        return max(0.0, self.scale - 0.5)

    def learn(self, panel, eps):
        """Correct the scale from a reading taken with our target in place.
        A reading at either end of the range says nothing. True if changed."""
        if self.target is None or panel is None or not eps < panel < 1.0 - eps:
            return False
        scale = min(1.0, max(0.0, panel - self.target + 0.5))
        if abs(scale - self.scale) <= eps:
            return False
        self.scale = scale
        return True

    def keys_landed(self, before, after, presses, eps):
        """Does the panel already show `presses` key steps away from
        `before`? A reading that does not is a key press still on its way
        to the hardware, or a slider."""
        if abs(after - before) <= eps:
            return False
        direction = 1 if after > before else -1
        scale = min(1.0, max(0.0, self.scale + direction * presses * self.step))
        return abs(self.panel(scale=scale) - after) <= max(2 * eps, self.step / 4)

    def user_change(self, before, after, presses, eps):
        """The shell reported a change by the user; `before` and `after` are
        panel readings. Updates the scale; returns (direction, keys)."""
        scale, step = self.scale, self.step
        if abs(after - before) > eps:
            direction = 1 if after > before else -1
        else:
            # Nothing visible: the scale is at an end, or the panel is.
            # Whichever press would have left the panel alone is the one made.
            up_same = abs(self.panel(scale=min(1.0, scale + step)) - before) <= eps
            down_same = abs(self.panel(scale=max(0.0, scale - step)) - before) <= eps
            direction = (1 if up_same and not down_same else
                         -1 if down_same and not up_same else 0)
        expected = min(1.0, max(0.0, scale + direction * presses * step))
        if self.target is not None and eps < after < 1.0 - eps:
            measured = min(1.0, max(0.0, after - self.target + 0.5))
        else:
            measured = expected
        keys = direction != 0 and abs(measured - expected) <= max(2 * eps, step / 4)
        self.scale = expected if keys else measured
        return direction, keys


def _tolerance(span):
    """Panel readings closer than this are the same, in 0-1 units."""
    if span <= 0:
        return 0.01
    return 0.5 / span if span <= 200 else max(1.5 / span, 0.0005)


class _ScaleStore:
    """The shell's scale as we last knew it, kept across sessions like the
    shell keeps it, so the first target at login is already right."""

    def __init__(self, path=None):
        self.path = path or (paths.state_dir() / "gnome-shell.json")

    def load(self, device):
        try:
            value = json.loads(self.path.read_text()).get(device)
            return None if value is None else min(1.0, max(0.0, float(value)))
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def save(self, device, scale):
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data[device] = round(scale, 6)
        try:
            paths.atomic_write(self.path, json.dumps(data))
        except OSError as error:
            log.debug("could not save the shell's scale: %s", error)


class GnomeShellMethod:
    name = "gnome-shell"
    BUS = "org.gnome.Shell.Brightness"
    PATH = "/org/gnome/Shell/Brightness"
    events = True
    # After the signal the panel is read every POLL seconds. A key press
    # counts as soon as the panel shows its step, which takes a few
    # milliseconds (the shell writes through Mutter and logind); until then
    # the panel shows the shell's own 5 % step, past ours. Anything else, a
    # slider being dragged or a press that changes nothing visible, waits
    # for SETTLE seconds without signals.
    POLL = 0.015
    SETTLE = 0.3

    def __init__(self, device, bus, store=None, clock=time.monotonic):
        self.device = device
        self.bus = bus
        self.store = store or _ScaleStore()
        self.clock = clock
        self.lock = threading.Lock()
        self.model = None
        self.limits = None
        self.verified = False
        self.note = ""
        self._pending = None

    def available(self):
        try:
            return bool(self.bus.get(self.BUS, self.PATH, self.BUS, "HasBrightnessControl"))
        except Exception:
            return False

    def _limits(self):
        """The range the shell maps 0-1 onto: Mutter's, which may start
        above zero (4000 of 400000 on an amdgpu panel)."""
        if self.limits is not None:
            return self.limits
        try:
            _, entries = self.bus.get(MutterMethod.BUS, MutterMethod.PATH,
                                      MutterMethod.BUS, "Backlight")
            entry = pick_mutter_entry(entries, self.device)
            if entry and entry["max"] > entry.get("min", 0):
                self.limits = (entry.get("min", 0), entry["max"])
                return self.limits
        except Exception as error:
            log.debug("no backlight range from mutter: %s", error)
        return (0, self.device.max_brightness)      # asked again next time

    @property
    def eps(self):
        low, high = self._limits()
        return _tolerance(high - low)

    def panel(self):
        """The panel now, 0-1 in the shell's units, from sysfs."""
        raw = self.device.read_raw()
        if raw is None:
            return None
        low, high = self._limits()
        return min(1.0, max(0.0, (raw - low) / (high - low)))

    def _model(self):
        if self.model is None:
            low, high = self._limits()
            scale = self.store.load(self.device.name)
            if scale is None:
                # No target yet, so the panel shows the scale. Checked again
                # once our target is in place, in case the shell had not
                # written it yet.
                scale = self.panel()
            self.model = ShellModel(0.5 if scale is None else scale,
                                    min(high - low, SHELL_STEPS))
        return self.model

    def write(self, level):
        with self.lock:
            model = self._model()
            wanted = level / 100.0
            target = model.target_for(wanted)
            trace.poke()
            log.debug("shell target: level %d %%, scale %.4f -> target %.4f", level,
                      model.scale, target)
            self.bus.call(self.BUS, self.PATH, self.BUS, "SetAutoBrightnessTarget", "(d)",
                          (target,))
            model.target = target
            note = ""
            if wanted < model.lowest() - self.eps:
                note = (f"GNOME's brightness setting keeps the panel at "
                        f"{clamp_level(model.lowest() * 100)} % or more; "
                        f"press brightness down to go lower")
            if note and note != self.note:
                log.warning("%s", note)
            self.note = note

    def read(self):
        value = self.panel()
        return None if value is None else clamp_level(value * 100)

    def check(self):
        """Compare the panel with the model once our first target is in
        place. Call only while the user is active: when idle, the shell may
        be dimming the panel. True when the scale had to be corrected."""
        if self.verified or self.model is None or self.model.target is None:
            return False
        if self._pending is not None:
            return False                  # a key press is on its way
        value = self.panel()
        log.debug("shell check: panel %s, model scale %.4f target %.4f", value,
                  self.model.scale, self.model.target)
        with self.lock:
            self.verified = True
            before = self.model.scale
            changed = self.model.learn(value, self.eps)
            scale = self.model.scale
        if changed:
            log.info("GNOME's brightness scale is %.0f %%, not %.0f %%; correcting",
                     scale * 100, before * 100)
        self.store.save(self.device.name, scale)
        return changed

    def subscribe(self, scheduler, handler):
        def finish(after):
            before, presses = self._pending["before"], self._pending["presses"]
            self._pending = None
            if after is None or self.model is None:
                return
            with self.lock:
                direction, keys = self.model.user_change(before, after, presses, self.eps)
                scale = self.model.scale
            if self.eps < after < 1.0 - self.eps:
                self.verified = True      # the reading pinned the scale down
            self.store.save(self.device.name, scale)
            log.info("GNOME brightness changed by the user: panel %.3f -> %.3f, "
                      "%d signal(s), direction %d, %s, scale now %.3f", before, after,
                      presses, direction, "keys" if keys else "slider", scale)
            if direction:
                handler(UserChange(clamp_level(after * 100), direction, presses, keys))

        def poll():
            pending = self._pending
            if pending is None:
                return
            pending["timer"] = None
            after = self.panel()
            landed = False
            if after is not None and self.model is not None:
                with self.lock:
                    landed = self.model.keys_landed(pending["before"], after,
                                                    pending["presses"], self.eps)
            quiet = self.clock() - pending["last"]
            log.debug("shell poll: panel %s (raw %s), before %.4f, presses %d, landed %s, "
                      "quiet %.3f s", None if after is None else f"{after:.4f}",
                      self.device.read_raw(), pending["before"], pending["presses"], landed,
                      quiet)
            if landed:
                finish(after)
                return
            if quiet >= self.SETTLE:
                finish(after)
                return
            pending["timer"] = scheduler.timeout(self.POLL, poll)

        def on_signal(_args):
            trace.poke()
            if self.model is None or self.model.target is None:
                log.debug("shell signal before our first target")
                # Not under our control yet; the scale is checked after
                # our first target.
                self.verified = False
                return
            log.debug("shell signal: %s, model scale %.4f target %.4f panel %.4f",
                      "new" if self._pending is None else
                      f"pending ({self._pending['presses']} so far)",
                      self.model.scale, self.model.target, self.model.panel())
            if self._pending is None:
                self._pending = {"before": self.model.panel(), "presses": 0, "timer": None}
            self._pending["presses"] += 1
            self._pending["last"] = self.clock()
            if self._pending["timer"] is None:
                self._pending["timer"] = scheduler.timeout(self.POLL, poll)

        return self.bus.subscribe(self.BUS, self.BUS, "BrightnessChanged", self.PATH,
                                  on_signal)

    def release(self):
        """Hand the backlight back to the shell's own scale."""
        try:
            self.bus.call(self.BUS, self.PATH, self.BUS, "SetAutoBrightnessTarget", "(d)",
                          (-1.0,))
        except Exception as error:
            log.debug("could not release the shell's target: %s", error)


def owner_command(bus, name, proc="/proc"):
    """The command name (comm) of the process that owns a bus name, or ""."""
    try:
        pid = bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                       "GetConnectionUnixProcessID", "(s)", (name,), "(u)")[0]
        with open(os.path.join(proc, str(pid), "comm"), encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return ""


class MutterMethod:
    name = "mutter"
    BUS = "org.gnome.Mutter.DisplayConfig"
    PATH = "/org/gnome/Mutter/DisplayConfig"
    PROC = "/proc"

    def __init__(self, device, bus):
        self.device = device
        self.bus = bus

    def _entry(self):
        serial, entries = self.bus.get(self.BUS, self.PATH, self.BUS, "Backlight")
        return serial, pick_mutter_entry(entries, self.device)

    def available(self):
        # Inside GNOME Shell, Mutter's interface comes up a quarter of a
        # second before the shell's own (seen at login on GNOME 51). Writing
        # through Mutter in between would move the shell's scale from outside,
        # the very thing the gnome-shell method avoids, so wait for the shell.
        # Mutter on its own (another compositor built on it) is fine.
        if owner_command(self.bus, self.BUS, self.PROC) == "gnome-shell":
            return False
        try:
            return self._entry()[1] is not None
        except Exception:
            return False

    def write(self, level):
        serial, entry = self._entry()
        if entry is None:
            raise RuntimeError("mutter reports no controllable backlight")
        value = percent_to_raw(level, entry["max"], entry.get("min", 0))
        self.bus.call(self.BUS, self.PATH, self.BUS, "SetBacklight", "(usi)",
                      (serial, entry["connector"], value))

    def read(self):
        _, entry = self._entry()
        if entry is None or "value" not in entry:
            return None
        return raw_to_percent(entry["value"], entry["max"], entry.get("min", 0))


class KdeMethod:
    name = "kde"
    BUS = "org.kde.ScreenBrightness"
    PATH = "/org/kde/ScreenBrightness"
    DISPLAY = "org.kde.ScreenBrightness.Display"
    SUPPRESS_INDICATOR = 0x1

    events = False        # keys come as signals, other changes still by polling

    def __init__(self, device, bus):
        self.device = device
        self.bus = bus
        self.display_name = None      # "display0"
        self.last = None              # (value, maximum) last seen

    def _display(self):
        names = self.bus.get(self.BUS, self.PATH, self.BUS, "DisplaysDBusNames")
        candidates = []
        for name in names:
            path = f"{self.PATH}/{name}"
            props = self.bus.call(self.BUS, path, "org.freedesktop.DBus.Properties",
                                  "GetAll", "(s)", (self.DISPLAY,), "(a{sv})")[0]
            candidates.append((not props.get("IsInternal", False), name, path, props))
        candidates.sort(key=lambda c: c[0])
        if not candidates or candidates[0][0]:
            return None, None
        _, self.display_name, path, props = candidates[0]
        return path, props

    def subscribe(self, scheduler, handler):
        """Plasma names the source of every change; the keys say
        "brightness_key" (seen on Plasma 6.8). Other sources, its own idle
        dimming among them, are left to the poll."""
        def on_signal(args):
            display, value, client, context = args
            trace.poke()
            log.debug("kde signal: %s %s from %s (%s), last %s", display, value, client,
                      context or "-", self.last)
            if context != "brightness_key" or display != self.display_name or not self.last:
                return
            previous, maximum = self.last
            self.last = (value, maximum)
            if value != previous:
                direction = 1 if value > previous else -1
            else:
                direction = 1 if value >= maximum else -1 if value <= 0 else 0
            log.debug("kde keys: %s -> %s, direction %d", previous, value, direction)
            if direction:
                handler(UserChange(clamp_level(value * 100.0 / maximum), direction, 1, True))

        return self.bus.subscribe(self.BUS, self.BUS, "BrightnessChanged", self.PATH,
                                  on_signal)

    def available(self):
        try:
            return self.bus.has_owner(self.BUS) and self._display()[0] is not None
        except Exception:
            return False

    def write(self, level):
        path, props = self._display()
        if path is None:
            raise RuntimeError("Plasma reports no built-in display")
        maximum = props.get("MaxBrightness", 100)
        # Plasma's 0 is "dimmest still visible", not off.
        value = max(0, min(maximum, round(maximum * level / 100.0)))
        trace.poke()
        log.debug("kde write: level %d %% -> %d of %d (was %s)", level, value, maximum,
                  props.get("Brightness"))
        self.bus.call(self.BUS, path, self.DISPLAY, "SetBrightness", "(iu)",
                      (value, self.SUPPRESS_INDICATOR))
        self.last = (value, maximum)

    def read(self):
        path, props = self._display()
        if path is None:
            return None
        maximum = props.get("MaxBrightness", 0)
        if maximum <= 0:
            return None
        return clamp_level(props.get("Brightness", 0) * 100.0 / maximum)


class LogindMethod:
    name = "logind"

    def __init__(self, device, bus):
        self.device = device
        self.bus = bus

    def available(self):
        return self.bus is not None and self.bus.has_owner("org.freedesktop.login1")

    def write(self, level):
        raw = percent_to_raw(level, self.device.max_brightness)
        # "auto" resolves to the caller's session, or for a user service
        # outside any session, to the user's display session.
        self.bus.call("org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
                      "org.freedesktop.login1.Session", "SetBrightness", "(ssu)",
                      ("backlight", self.device.name, raw))

    def read(self):
        raw = self.device.read_raw()
        return None if raw is None else raw_to_percent(raw, self.device.max_brightness)


class SysfsMethod:
    name = "sysfs"

    def __init__(self, device, bus=None):
        self.device = device

    def available(self):
        return os.access(self.device.path / "brightness", os.W_OK)

    def write(self, level):
        raw = percent_to_raw(level, self.device.max_brightness)
        (self.device.path / "brightness").write_text(str(raw))

    def read(self):
        raw = self.device.read_raw()
        return None if raw is None else raw_to_percent(raw, self.device.max_brightness)


METHOD_ORDER = ("gnome-shell", "mutter", "kde", "logind", "sysfs")
# Bus names whose arrival or departure may change the method "auto" picks.
DESKTOP_NAMES = (GnomeShellMethod.BUS, MutterMethod.BUS, KdeMethod.BUS)


class BacklightActuator:
    kind = "backlight"
    readable = True

    def __init__(self, device, method="auto", dry_run=False, session_bus=None,
                 system_bus=None, store=None):
        self.device = device
        self.dry_run = dry_run
        self.requested = method
        self._session_bus = session_bus
        self._system_bus = system_bus
        self._store = store
        self.method = None
        self._dry_level = None
        self._watch = None            # (scheduler, handler)
        self._unsubscribe = None
        self._lock = threading.RLock()

    @property
    def events(self):
        """True when the method reports every user change itself, so the
        panel need not be polled for the brightness keys."""
        return bool(getattr(self.method, "events", False))

    @property
    def note(self):
        return getattr(self.method, "note", "")

    def watch(self, scheduler, handler):
        """Report brightness changes made through the desktop as they happen,
        as UserChange objects; handler runs in the main loop."""
        with self._lock:
            self._watch = (scheduler, handler)
            self._resubscribe()

    def _resubscribe(self):
        if self._unsubscribe:
            try:
                self._unsubscribe()
            except Exception:
                pass
            self._unsubscribe = None
        if self._watch and self.method is not None and hasattr(self.method, "subscribe"):
            try:
                self._unsubscribe = self.method.subscribe(*self._watch)
            except Exception as error:
                log.debug("no brightness signals from %s: %s", self.method.name, error)

    def check(self):
        """See whether the method's idea of the hardware still holds; True
        when the level must be written again."""
        check = getattr(self.method, "check", None)
        return bool(check and check())

    def close(self):
        with self._lock:
            self._watch = None
            self._resubscribe()

    def _buses(self):
        if self._session_bus is None:
            try:
                self._session_bus = _Bus(system=False)
            except Exception as error:
                log.debug("no session bus: %s", error)
        if self._system_bus is None:
            try:
                self._system_bus = _Bus(system=True)
            except Exception as error:
                log.debug("no system bus: %s", error)
        return self._session_bus, self._system_bus

    def _make(self, name):
        session, system = self._buses()
        if name == "gnome-shell":
            return GnomeShellMethod(self.device, session, self._store) if session else None
        if name == "mutter":
            return MutterMethod(self.device, session) if session else None
        if name == "kde":
            return KdeMethod(self.device, session) if session else None
        if name == "logind":
            return LogindMethod(self.device, system) if system else None
        return SysfsMethod(self.device)

    def select(self, exclude=()):
        """Pick the first method that is available, honouring the setting.
        Keeps the current method object, and what it learned, when it wins
        again. Returns the method."""
        names = METHOD_ORDER if self.requested == "auto" else (self.requested,)
        with self._lock:
            chosen = None
            for name in names:
                if name in exclude:
                    continue
                if self.method is not None and self.method.name == name:
                    method = self.method
                else:
                    method = self._make(name)
                if method is not None and method.available():
                    chosen = method
                    break
            if chosen is self.method:
                return chosen
            previous, self.method = self.method, chosen
            if previous is not None and hasattr(previous, "release") \
                    and previous.name not in exclude:
                previous.release()
            if chosen is not None:
                log.info("backlight %s: using %s", self.device.name, chosen.name)
            self._resubscribe()
            return chosen

    def describe(self):
        method = self.method.name if self.method else "no method"
        return f"{self.device.name} via {method}"

    def write(self, level, repeats=None):
        if self.dry_run:
            log.info("[dry run] backlight %s -> %d%%", self.device.name, level)
            self._dry_level = level
            return True
        tried = []
        method = self.method or self.select()
        trace.poke()
        while method is not None:
            try:
                method.write(level)
                return True
            except Exception as error:
                log.warning("backlight via %s failed: %s", method.name, error)
                tried.append(method.name)
                if self.requested != "auto":
                    return False
                method = self.select(exclude=tried)
        return False

    def read(self):
        if self.dry_run:
            return self._dry_level
        method = self.method or self.select()
        try:
            return method.read() if method else None
        except Exception as error:
            log.debug("backlight read via %s failed: %s", method.name, error)
            return None


def session_idle_ms(bus):
    """Milliseconds since the last user input, or None when unknown.

    Lets us tell the brightness keys (user active) from the desktop's own idle
    dimming (user away) when the backlight changes under us.
    """
    if bus is None:
        return None
    try:
        return int(bus.call("org.gnome.Mutter.IdleMonitor",
                            "/org/gnome/Mutter/IdleMonitor/Core",
                            "org.gnome.Mutter.IdleMonitor", "GetIdletime",
                            reply="(t)")[0])
    except Exception:
        pass
    try:
        # Plasma answers from KIdleTime, which counts in milliseconds.
        return int(bus.call("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                            "org.freedesktop.ScreenSaver", "GetSessionIdleTime",
                            reply="(u)")[0])
    except Exception:
        return None
