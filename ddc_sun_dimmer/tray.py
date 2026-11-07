"""Tray icon over StatusNotifierItem and com.canonical.dbusmenu, with Gio only.

libayatana-appindicator is GTK 3 and cannot share a process with GTK 4, and
the daemon should not load a toolkit at all. Both protocols are plain D-Bus,
so we speak them directly. Hosts: KDE Plasma, GNOME with the AppIndicator
extension, XFCE, Cinnamon, waybar and others.
"""

import logging
import os
import time

from gi.repository import Gio, GLib

from . import policy
from .prefs import load_ui_prefs

log = logging.getLogger(__name__)

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
LEVEL_PRESETS = (100, 75, 50, 25, 10, 1)
SCROLL_STEP = 5
EXTEND_MINUTES = 15

ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <property name="XAyatanaLabelGuide" type="s" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <method name="ProvideXdgActivationToken"><arg name="token" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <signal name="XAyatanaNewLabel"><arg name="label" type="s"/><arg name="guide" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg name="id" type="i"/>
      <arg name="timestamp" type="u"/>
    </signal>
  </interface>
</node>
"""


# --- texts -------------------------------------------------------------------


def clock(timestamp):
    return time.strftime("%H:%M", time.localtime(timestamp))


def duration_label(minutes):
    if minutes < 60:
        return f"{minutes} minutes"
    hours, rest = divmod(minutes, 60)
    if rest == 0:
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{hours} h {rest} min"


def headline(status):
    if status.get("config_error"):
        return "Settings error"
    if "level" not in status:
        return "Starting…"
    level, mode, until = status["level"], status["mode"], status.get("until", 0)
    if mode == "paused":
        return (f"Paused at {level} % until {clock(until)}" if until
                else f"Paused at {level} %")
    if mode == "override":
        return f"Manual {level} % until {clock(until)}" if until else f"Manual {level} %"
    if mode == "preview":
        return f"Trying {level} %"
    return f"Brightness {level} %"


def detail_lines(status):
    lines = []
    if status.get("config_error"):
        lines.append(status["config_error"])
        return lines
    if status.get("mode") == "auto" and status.get("next_change"):
        lines.append(f"Next: {status['next_level']} % at {clock(status['next_change'])}")
    if status.get("weather_enabled"):
        cloud = status.get("cloud", -1)
        if cloud >= 0:
            change = round((status.get("factor", 1.0) - 1) * 100)
            lines.append(f"Cloud cover {cloud:.0f} %" + (f", {change:+d} %" if change else ""))
        elif status.get("weather_error"):
            lines.append("Weather unavailable")
    if status.get("sunrise") and status.get("sunset"):
        lines.append(f"Sunrise {clock(status['sunrise'])}, sunset {clock(status['sunset'])}")
    for output in status.get("outputs", []):
        if output.get("state") == "failed":
            lines.append(f"{output['label']}: not responding")
    return lines


def icon_name(status):
    if status.get("config_error"):
        return "dialog-warning-symbolic"
    mode = status.get("mode")
    if mode == "paused":
        return "media-playback-pause-symbolic"
    phase = status.get("phase")
    if phase == "night":
        return "weather-clear-night-symbolic"
    if phase == "day":
        if status.get("factor", 1.0) < 0.95:
            return "weather-few-clouds-symbolic"
        return "weather-clear-symbolic"
    return "display-brightness-symbolic"


# --- menu model --------------------------------------------------------------


class Item:
    def __init__(self, label="", action=None, enabled=True, separator=False,
                 toggle=None, checked=False, children=None, icon=None):
        self.id = 0
        self.label = label
        self.action = action
        self.enabled = enabled
        self.separator = separator
        self.toggle = toggle             # None, "radio" or "checkmark"
        self.checked = checked
        self.children = children or []
        self.icon = icon

    def properties(self):
        if self.separator:
            return {"type": GLib.Variant("s", "separator")}
        props = {"label": GLib.Variant("s", self.label.replace("_", "__")),
                 "enabled": GLib.Variant("b", self.enabled)}
        if self.toggle:
            props["toggle-type"] = GLib.Variant("s", self.toggle)
            props["toggle-state"] = GLib.Variant("i", 1 if self.checked else 0)
        if self.children:
            props["children-display"] = GLib.Variant("s", "submenu")
        if self.icon:
            props["icon-name"] = GLib.Variant("s", self.icon)
        return props

    def signature(self):
        return (self.label, self.enabled, self.separator, self.toggle, self.checked,
                self.icon, tuple(child.signature() for child in self.children))


def separator():
    return Item(separator=True)


def build_menu(status, actions, prefs):
    """The menu as a tree of Items for the current status."""
    a = actions
    items = [Item(headline(status), enabled=False)]
    items += [Item(line, enabled=False) for line in detail_lines(status)]
    if status.get("config_error"):
        items += [separator(), Item("Open settings…", a.open_settings)]
        return Item(children=items)

    mode = status.get("mode")
    level = status.get("level")
    items.append(separator())

    brightness = [Item(f"{preset} %", lambda p=preset: a.set_level(p, policy.NEXT_PHASE),
                       toggle="radio", checked=(mode in ("override", "paused")
                                                and level == preset))
                  for preset in LEVEL_PRESETS]
    brightness += [separator(),
                   Item(f"Brighter (+{SCROLL_STEP} %)", lambda: a.adjust(SCROLL_STEP)),
                   Item(f"Dimmer (−{SCROLL_STEP} %)", lambda: a.adjust(-SCROLL_STEP))]
    items.append(Item("Set brightness", children=brightness))

    if mode == "override":
        items.append(Item("Back to schedule", a.resume))
    if mode == "paused":
        items.append(Item("Resume now", a.resume))
        until = status.get("until", 0)
        if until:
            items.append(Item(f"Extend pause by {EXTEND_MINUTES} min",
                              lambda: a.pause(int(until + EXTEND_MINUTES * 60))))

    presets = status.get("pause_presets") or [15, 30, 60, 120]
    pause_items = [Item(duration_label(m), lambda m=m: a.pause(int(now_fn() + m * 60)))
                   for m in presets]
    last = prefs.get("last_pause_minutes")
    if isinstance(last, int) and last not in presets:
        pause_items.append(Item(f"{duration_label(last)} (last used)",
                                lambda: a.pause(int(now_fn() + last * 60))))
    next_phase = status.get("next_phase", 0)
    phase_label = "Until the next phase"
    if next_phase:
        phase_label += f" ({clock(next_phase)})"
    pause_items += [separator(),
                    Item(phase_label, lambda: a.pause(policy.NEXT_PHASE)),
                    Item("Until resumed", lambda: a.pause(policy.FOREVER)),
                    separator(),
                    Item("Custom…", a.open_pause_dialog)]
    items.append(Item("Change pause" if mode == "paused" else "Pause", children=pause_items))

    items += [separator(),
              Item("Settings…", a.open_settings),
              Item("Hide this icon", a.hide_tray)]
    return Item(children=items)


def now_fn():
    """Pause times are computed when clicked, not when the menu was built."""
    return time.time()


def number_items(root):
    """Assign ids depth-first; the root is 0. Returns {id: Item}."""
    table = {}
    counter = 0

    def walk(item):
        nonlocal counter
        item.id = counter
        table[counter] = item
        counter += 1
        for child in item.children:
            walk(child)

    walk(root)
    return table


def layout(item, depth, names=()):
    props = item.properties()
    if item.id == 0:
        props = {"children-display": GLib.Variant("s", "submenu")}
    if names:
        props = {k: v for k, v in props.items() if k in names}
    children = []
    if depth != 0:
        children = [GLib.Variant("(ia{sv}av)", layout(child, depth - 1, names))
                    for child in item.children]
    return item.id, props, children


# --- the D-Bus objects -------------------------------------------------------


class Actions:
    """Menu actions, routed to the daemon and guarded against exceptions."""

    def __init__(self, daemon):
        self.daemon = daemon

    def _run(self, function, *args):
        try:
            function(*args)
        except Exception as error:
            log.warning("tray action failed: %s", error)

    def set_level(self, level, until):
        self._run(self.daemon.set_level, level, until)

    def adjust(self, delta):
        self._run(self.daemon.adjust, delta)

    def pause(self, until):
        self._run(self.daemon.pause, until)

    def resume(self):
        self._run(self.daemon.resume)

    def open_settings(self):
        self._run(self.daemon.open_settings)

    def open_pause_dialog(self):
        self._run(self.daemon.open_settings, "--pause")

    def hide_tray(self):
        self._run(self.daemon.hide_tray)


class Tray:
    def __init__(self, connection, daemon):
        self.connection = connection
        self.daemon = daemon
        self.actions = Actions(daemon)
        self.status = {}
        self.revision = 1
        self.root = Item()
        self.items = {0: self.root}
        self._menu_signature = None
        self._icon = ""
        self._tooltip = None
        self._label = ""
        self._scroll_signs = {}
        self._said = set()
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

        item_node = Gio.DBusNodeInfo.new_for_xml(ITEM_XML)
        menu_node = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
        self._registrations = [
            connection.register_object(ITEM_PATH, item_node.interfaces[0],
                                       self._item_call, self._item_property, None),
            connection.register_object(MENU_PATH, menu_node.interfaces[0],
                                       self._menu_call, self._menu_property, None),
        ]
        self._owner = Gio.bus_own_name_on_connection(
            connection, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        self._watch = Gio.bus_watch_name_on_connection(
            connection, WATCHER, Gio.BusNameWatcherFlags.NONE,
            self._watcher_appeared, self._watcher_vanished)

    def close(self):
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._owner)
        for registration in self._registrations:
            self.connection.unregister_object(registration)

    # --- registration ------------------------------------------------------

    def _watcher_appeared(self, connection, name, owner):
        def done(conn, result):
            try:
                conn.call_finish(result)
                self._say("tray icon registered")
            except GLib.Error as error:
                log.warning("tray host refused the icon: %s", error.message)
        connection.call(WATCHER, "/StatusNotifierWatcher", WATCHER,
                        "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.name,)),
                        None, Gio.DBusCallFlags.NONE, 5000, None, done)

    def _watcher_vanished(self, connection, name):
        # GNOME disables extensions, the tray host among them, whenever the
        # screen locks; say so once, not at every lock.
        self._say("no tray host on the session bus (on GNOME this needs the "
                  "AppIndicator extension); the icon appears when one starts")

    def _say(self, message):
        if message in self._said:
            log.debug(message)
        else:
            self._said.add(message)
            log.info(message)

    # --- updates -----------------------------------------------------------

    def update(self, status):
        self.status = status
        self._rebuild()
        icon = icon_name(status)
        if icon != self._icon:
            self._icon = icon
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")
        tooltip = self._tooltip_value()
        if tooltip != self._tooltip:
            self._tooltip = tooltip
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")
        label = f"{status['level']}%" if status.get("show_level") and "level" in status else ""
        if label != self._label:
            self._label = label
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "XAyatanaNewLabel",
                       GLib.Variant("(ss)", (label, "100%")))

    def _rebuild(self):
        root = build_menu(self.status, self.actions, load_ui_prefs())
        signature = root.signature()
        if signature == self._menu_signature:
            return False
        self._menu_signature = signature
        self.root = root
        self.items = number_items(root)
        self.revision += 1
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                   GLib.Variant("(ui)", (self.revision, 0)))
        return True

    def _emit(self, path, interface, name, params=None):
        try:
            self.connection.emit_signal(None, path, interface, name, params)
        except GLib.Error as error:
            log.debug("signal %s failed: %s", name, error.message)

    def _tooltip_value(self):
        return (self._icon, [], headline(self.status), "\n".join(detail_lines(self.status)))

    # --- StatusNotifierItem --------------------------------------------------

    def _item_property(self, connection, sender, path, interface, name):
        values = {
            "Category": ("s", "Hardware"),
            "Id": ("s", "ddc-sun-dimmer"),
            "Title": ("s", "Display brightness"),
            "Status": ("s", "Active"),
            "WindowId": ("i", 0),
            "IconName": ("s", self._icon or icon_name(self.status)),
            "IconPixmap": ("a(iiay)", []),
            "OverlayIconName": ("s", ""),
            "OverlayIconPixmap": ("a(iiay)", []),
            "AttentionIconName": ("s", ""),
            "AttentionIconPixmap": ("a(iiay)", []),
            "AttentionMovieName": ("s", ""),
            "IconThemePath": ("s", ""),
            "ToolTip": ("(sa(iiay)ss)", self._tooltip or self._tooltip_value()),
            "ItemIsMenu": ("b", False),
            "Menu": ("o", MENU_PATH),
            "XAyatanaLabel": ("s", self._label),
            "XAyatanaLabelGuide": ("s", "100%"),
        }
        kind, value = values[name]
        return GLib.Variant(kind, value)

    def _item_call(self, connection, sender, path, interface, method, params, invocation):
        invocation.return_value(None)
        if method == "Activate":
            self.actions.open_settings()
        elif method == "SecondaryActivate":
            # Middle click: toggle between the schedule and a pause.
            if self.status.get("mode") in ("paused", "override"):
                self.actions.resume()
            else:
                self.actions.pause(policy.FOREVER)
        elif method == "Scroll":
            delta, orientation = params.unpack()
            if orientation.lower() == "vertical" and delta:
                direction = 1 if delta > 0 else -1
                self.actions.adjust(direction * self._scroll_sign(sender) * SCROLL_STEP)

    def _scroll_sign(self, sender):
        """+1 when positive deltas mean "up". GNOME Shell sends Clutter's
        convention, where scrolling down is positive; Plasma sends Qt's."""
        if sender not in self._scroll_signs:
            sign = 1
            try:
                pid = self.connection.call_sync(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                    "GetConnectionUnixProcessID", GLib.Variant("(s)", (sender,)),
                    GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
                with open(f"/proc/{pid}/comm") as handle:
                    if handle.read().strip().startswith("gnome-shell"):
                        sign = -1
            except (GLib.Error, OSError):
                pass
            self._scroll_signs[sender] = sign
        return self._scroll_signs[sender]

    # --- dbusmenu ----------------------------------------------------------

    def _menu_property(self, connection, sender, path, interface, name):
        values = {"Version": ("u", 3), "TextDirection": ("s", "ltr"),
                  "Status": ("s", "normal"), "IconThemePath": ("as", [])}
        kind, value = values[name]
        return GLib.Variant(kind, value)

    def _menu_call(self, connection, sender, path, interface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent, depth, names = args
            item = self.items.get(parent, self.root)
            invocation.return_value(GLib.Variant(
                "(u(ia{sv}av))", (self.revision, layout(item, depth, tuple(names)))))
        elif method == "GetGroupProperties":
            ids, names = args
            chosen = [self.items[i] for i in ids if i in self.items] if ids \
                else list(self.items.values())
            result = []
            for item in chosen:
                props = item.properties() if item.id else \
                    {"children-display": GLib.Variant("s", "submenu")}
                if names:
                    props = {k: v for k, v in props.items() if k in names}
                result.append((item.id, props))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))
        elif method == "GetProperty":
            item_id, name = args
            props = self.items[item_id].properties() if item_id in self.items else {}
            if name in props:
                invocation.return_value(GLib.Variant("(v)", (props[name],)))
            else:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error.InvalidProperty",
                                             name)
        elif method == "Event":
            item_id, event, _data, _timestamp = args
            invocation.return_value(None)
            self._activate(item_id, event)
        elif method == "EventGroup":
            errors = []
            for item_id, event, _data, _timestamp in args[0]:
                if item_id not in self.items:
                    errors.append(item_id)
                else:
                    self._activate(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            # The menu is about to open: refresh times such as "Next: … at".
            changed = self._rebuild() if args[0] == 0 else False
            invocation.return_value(GLib.Variant("(b)", (changed,)))
        elif method == "AboutToShowGroup":
            changed = self._rebuild() if 0 in args[0] else False
            invocation.return_value(GLib.Variant("(aiai)", ([0] if changed else [], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _activate(self, item_id, event):
        item = self.items.get(item_id)
        if event != "clicked" or item is None or item.action is None or not item.enabled:
            return
        action = item.action

        def run():
            action()
            return False
        GLib.idle_add(run)
