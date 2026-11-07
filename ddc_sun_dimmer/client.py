"""Talking to the daemon over the session bus."""

from gi.repository import Gio, GLib

from . import BUS_NAME, INTERFACE, OBJECT_PATH


class DaemonUnavailable(RuntimeError):
    pass


class DaemonError(RuntimeError):
    pass


class Client:
    def __init__(self, connection=None):
        try:
            self.connection = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as error:
            raise DaemonUnavailable(f"no session bus: {error.message}") from None

    def available(self):
        try:
            reply = self.connection.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "NameHasOwner", GLib.Variant("(s)", (BUS_NAME,)), GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE, 2000, None)
            return reply.unpack()[0]
        except GLib.Error:
            return False

    def call(self, method, signature=None, args=(), reply=None, timeout=10000):
        params = GLib.Variant(signature, args) if signature else None
        try:
            result = self.connection.call_sync(
                BUS_NAME, OBJECT_PATH, INTERFACE, method, params,
                GLib.VariantType(reply) if reply else None,
                Gio.DBusCallFlags.NO_AUTO_START, timeout, None)
        except GLib.Error as error:
            remote = Gio.DBusError.get_remote_error(error)
            if remote in ("org.freedesktop.DBus.Error.ServiceUnknown",
                          "org.freedesktop.DBus.Error.NameHasNoOwner"):
                raise DaemonUnavailable("the ddc-sun-dimmer daemon is not running") from None
            Gio.DBusError.strip_remote_error(error)
            raise DaemonError(error.message) from None
        return result.unpack() if result is not None else ()

    def status(self):
        return self.call("GetStatus", reply="(a{sv})")[0]

    def set_level(self, level, until):
        self.call("SetLevel", "(ix)", (int(level), int(until)))

    def adjust(self, delta):
        self.call("Adjust", "(i)", (int(delta),))

    def pause(self, until):
        self.call("Pause", "(x)", (int(until),))

    def resume(self):
        self.call("Resume")

    def preview(self, level):
        self.call("Preview", "(i)", (int(level),))

    def end_preview(self):
        self.call("EndPreview")

    def reload(self):
        self.call("Reload")

    def refresh_weather(self):
        self.call("RefreshWeather")

    def forecast(self):
        fetched_at, points = self.call("GetForecast", reply="(da(xd))")
        return fetched_at, points

    def subscribe(self, callback):
        """Call `callback(status_dict)` whenever the daemon reports a change."""
        def on_signal(connection, sender, path, interface, name, params):
            callback(params.unpack()[0])
        return self.connection.signal_subscribe(
            BUS_NAME, INTERFACE, "Changed", OBJECT_PATH, None,
            Gio.DBusSignalFlags.NONE, on_signal)

    def watch(self, appeared, vanished):
        """Follow the daemon starting and stopping."""
        return Gio.bus_watch_name_on_connection(
            self.connection, BUS_NAME, Gio.BusNameWatcherFlags.NONE,
            lambda *a: appeared(), lambda *a: vanished())
