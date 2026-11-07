"""Built-in laptop panels.

Writing /sys/class/backlight directly works, but on a modern desktop it goes
behind the back of whoever owns the brightness: the desktop's slider stops
matching the screen, and the desktop restores its own value after idle
dimming or a resume. So the desktop's own interface comes first:

    mutter  GNOME 49 and later: org.gnome.Mutter.DisplayConfig.SetBacklight
    kde     Plasma 6: org.kde.ScreenBrightness
    logind  everything else (sway, Hyprland, niri, XFCE...): Session.SetBrightness,
            no root needed
    sysfs   last resort, needs write permission on the brightness file

Reading always works, which lets us notice the brightness keys.
"""

import logging
import os

from .. import clamp_level

log = logging.getLogger(__name__)

TIMEOUT_MS = 3000


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


class MutterMethod:
    name = "mutter"
    BUS = "org.gnome.Mutter.DisplayConfig"
    PATH = "/org/gnome/Mutter/DisplayConfig"

    def __init__(self, device, bus):
        self.device = device
        self.bus = bus

    def _entry(self):
        serial, entries = self.bus.get(self.BUS, self.PATH, self.BUS, "Backlight")
        usable = [e for e in entries if "max" in e and "connector" in e]
        wanted = self.device.connector.short if self.device.connector else None
        for entry in usable:
            if entry["connector"] == wanted:
                return serial, entry
        for entry in usable:
            if entry["connector"].startswith(("eDP", "LVDS", "DSI")):
                return serial, entry
        return (serial, usable[0]) if usable else (serial, None)

    def available(self):
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

    def __init__(self, device, bus):
        self.device = device
        self.bus = bus

    def _display(self):
        names = self.bus.get(self.BUS, self.PATH, self.BUS, "DisplaysDBusNames")
        candidates = []
        for name in names:
            path = f"{self.PATH}/{name}"
            props = self.bus.call(self.BUS, path, "org.freedesktop.DBus.Properties",
                                  "GetAll", "(s)", (self.DISPLAY,), "(a{sv})")[0]
            candidates.append((not props.get("IsInternal", False), path, props))
        candidates.sort(key=lambda c: c[0])
        if not candidates or candidates[0][0]:
            return None, None
        return candidates[0][1], candidates[0][2]

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
        self.bus.call(self.BUS, path, self.DISPLAY, "SetBrightness", "(iu)",
                      (value, self.SUPPRESS_INDICATOR))

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


METHOD_ORDER = ("mutter", "kde", "logind", "sysfs")


class BacklightActuator:
    kind = "backlight"
    readable = True

    def __init__(self, device, method="auto", dry_run=False, session_bus=None,
                 system_bus=None):
        self.device = device
        self.dry_run = dry_run
        self.requested = method
        self._session_bus = session_bus
        self._system_bus = system_bus
        self.method = None
        self._dry_level = None

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
        if name == "mutter":
            return MutterMethod(self.device, session) if session else None
        if name == "kde":
            return KdeMethod(self.device, session) if session else None
        if name == "logind":
            return LogindMethod(self.device, system) if system else None
        return SysfsMethod(self.device)

    def select(self, exclude=()):
        """Pick the first method that is available, honouring the setting."""
        names = METHOD_ORDER if self.requested == "auto" else (self.requested,)
        for name in names:
            if name in exclude:
                continue
            method = self._make(name)
            if method is not None and method.available():
                self.method = method
                log.info("backlight %s: using %s", self.device.name, name)
                return method
        self.method = None
        return None

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
