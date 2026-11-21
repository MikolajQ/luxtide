"""A stand-in for GNOME Shell 51's brightness, as brightnessManager.js does it.

Owns org.gnome.Shell.Brightness and org.gnome.Mutter.DisplayConfig on the
session bus and writes the panel to a fake sysfs file. A control interface
presses keys: test.Press(i direction).

    python3 fake_shell.py SYSFS_BRIGHTNESS_FILE INITIAL_SCALE
"""
import sys

from gi.repository import Gio, GLib

path, scale = sys.argv[1], float(sys.argv[2])
LOW, HIGH = 4000, 400000
state = {"scale": scale, "target": -1.0, "dim": False}

XML = """
<node>
  <interface name="org.gnome.Shell.Brightness">
    <method name="SetAutoBrightnessTarget"><arg type="d" direction="in"/></method>
    <method name="SetDimming"><arg type="b" direction="in"/></method>
    <signal name="BrightnessChanged"/>
    <property name="HasBrightnessControl" type="b" access="read"/>
  </interface>
  <interface name="org.gnome.Mutter.DisplayConfig">
    <property name="Backlight" type="(uaa{sv})" access="read"/>
  </interface>
  <interface name="test.Control">
    <method name="Press"><arg type="i" direction="in"/></method>
    <method name="Scale"><arg type="d" direction="out"/></method>
  </interface>
</node>"""


def sync():
    s, t = state["scale"], state["target"]
    panel = s if t < 0 else min(1.0, max(0.0, t + s - 0.5))
    if state["dim"]:
        panel = min(0.3, panel)
    with open(path, "w") as handle:
        handle.write(f"{int(LOW + (HIGH - LOW) * panel)}\n")


def method(conn, sender, obj, iface, name, params, inv):
    args = params.unpack()
    if name == "SetAutoBrightnessTarget":
        state["target"] = args[0]
        print(f"fake shell: target {args[0]:.4f}", flush=True)
        sync()
    elif name == "SetDimming":
        state["dim"] = args[0]
        sync()
    elif name == "Press":
        # The shell emits the signal right after asking Mutter for the new
        # backlight, which reaches sysfs a few milliseconds later, through
        # logind.
        state["scale"] = min(1.0, max(0.0, state["scale"] + args[0] / 20))
        conn.emit_signal(None, "/org/gnome/Shell/Brightness", "org.gnome.Shell.Brightness",
                         "BrightnessChanged", None)
        GLib.timeout_add(8, lambda: (sync(), False)[1])
    elif name == "Scale":
        inv.return_value(GLib.Variant("(d)", (state["scale"],)))
        return
    inv.return_value(None)


def prop(conn, sender, obj, iface, name):
    if name == "HasBrightnessControl":
        return GLib.Variant("b", True)
    if name == "Backlight":
        return GLib.Variant("(uaa{sv})", (1, [{
            "connector": GLib.Variant("s", "eDP-1"), "active": GLib.Variant("b", True),
            "min": GLib.Variant("i", LOW), "max": GLib.Variant("i", HIGH),
            "value": GLib.Variant("i", 0)}]))


bus = Gio.bus_get_sync(Gio.BusType.SESSION)
node = Gio.DBusNodeInfo.new_for_xml(XML)
bus.register_object("/org/gnome/Shell/Brightness", node.interfaces[0], method, prop, None)
bus.register_object("/org/gnome/Mutter/DisplayConfig", node.interfaces[1], method, prop, None)
bus.register_object("/test", node.interfaces[2], method, prop, None)
sync()          # what the shell does at start: the saved scale
for name in ("org.gnome.Mutter.DisplayConfig", "org.gnome.Shell.Brightness", "test.Shell"):
    Gio.bus_own_name_on_connection(bus, name, Gio.BusNameOwnerFlags.NONE, None, None)
print("fake shell up", flush=True)
GLib.MainLoop().run()
