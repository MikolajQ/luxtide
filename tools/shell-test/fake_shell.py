"""A stand-in for GNOME Shell 51's brightness, as brightnessManager.js does it.

Owns org.gnome.Shell.Brightness and org.gnome.Mutter.DisplayConfig on the
session bus and writes the panel to a fake sysfs file. A control interface
presses keys: test.Press(i direction).

    python3 fake_shell.py SYSFS_BRIGHTNESS_FILE INITIAL_SCALE|saved

"saved" reads the scale of eDP-1 from $XDG_DATA_HOME/gnome-shell/brightnesses,
as the shell does. The panel it starts at goes to SYSFS_BRIGHTNESS_FILE.start.
Like the shell, it saves its scale there 300 ms after it starts and after
every press.
"""
import os
import sys

from gi.repository import Gio, GLib

path = sys.argv[1]
saved = os.path.join(os.environ["XDG_DATA_HOME"], "gnome-shell", "brightnesses")
STATE = GLib.VariantType("a(sssssd)")


def load():
    with open(saved, "rb") as handle:
        return GLib.Variant.new_from_bytes(STATE, GLib.Bytes.new(handle.read()), False).unpack()


if sys.argv[2] == "saved":
    scale = next(entry[5] for entry in load() if entry[4] == "eDP-1")
else:
    scale = float(sys.argv[2])
LOW, HIGH = 4000, 400000
# "global" is what the keys move, "monitor" what the panel follows; they part
# only through the misreading below.
state = {"global": scale, "monitor": scale, "target": -1.0, "dim": False, "current": None,
         "save": 0}
WRITE_MS, CONFIRM_MS = 8, 30

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


def write_sysfs(panel):
    with open(path, "w") as handle:
        handle.write(f"{int(LOW + (HIGH - LOW) * panel)}\n")
    return False


def confirmed(panel):
    # Mutter confirms each write asynchronously. A confirmed value other than
    # the one the shell set last is taken for a change from outside and made
    # the monitor's scale (syncWithBacklight), as seen on a laptop.
    if state["current"] is not None and abs(panel - state["current"]) > 1e-9:
        print(f"fake shell: confirmed {panel:.3f} after {state['current']:.3f}; "
              f"the scale becomes {panel:.3f}", flush=True)
        state["monitor"] = panel
        sync(immediately=True)      # one misreading per write, no endless echo
    return False


def set_backlight(panel, immediately=False):
    if state["current"] is not None and abs(panel - state["current"]) < 1e-9:
        return
    state["current"] = panel
    if immediately:
        write_sysfs(panel)
    else:
        GLib.timeout_add(WRITE_MS, write_sysfs, panel)
        GLib.timeout_add(CONFIRM_MS, confirmed, panel)


def sync(immediately=False):
    s, t = state["monitor"], state["target"]
    panel = s if t < 0 else min(1.0, max(0.0, t + s - 0.5))
    if state["dim"]:
        panel = min(0.3, panel)
    set_backlight(panel, immediately)


def save_now():
    # _saveBrightnesses: the monitor's entry goes last, the others stay.
    state["save"] = 0
    try:
        entries = load()
    except (OSError, GLib.Error):
        entries = []
    ours = next((e[:5] for e in entries if e[4] == "eDP-1"),
                ("BOE", "0x0b0e", "0x00000000", "default", "eDP-1"))
    entries = [e for e in entries if e[:5] != ours] + [(*ours, state["monitor"])]
    data = GLib.Variant("a(sssssd)", entries).get_data_as_bytes().get_data()
    with open(saved + ".tmp", "wb") as handle:
        handle.write(data)
    os.replace(saved + ".tmp", saved)
    return False


def save():
    if state["save"]:
        GLib.source_remove(state["save"])
    state["save"] = GLib.timeout_add(300, save_now)


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
        # The keys move the global scale, which the monitor follows; the
        # signal comes right after the backlight was asked for.
        state["global"] = min(1.0, max(0.0, state["global"] + args[0] / 20))
        state["monitor"] = state["global"]
        sync()
        save()
        conn.emit_signal(None, "/org/gnome/Shell/Brightness", "org.gnome.Shell.Brightness",
                         "BrightnessChanged", None)
    elif name == "Scale":
        inv.return_value(GLib.Variant("(d)", (state["global"],)))
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
sync(immediately=True)          # what the shell does at start: the saved scale
save()
with open(path + ".start", "w") as handle:
    handle.write(f"{scale}\n")
for name in ("org.gnome.Mutter.DisplayConfig", "org.gnome.Shell.Brightness", "test.Shell"):
    Gio.bus_own_name_on_connection(bus, name, Gio.BusNameOwnerFlags.NONE, None, None)
print("fake shell up", flush=True)
GLib.MainLoop().run()
