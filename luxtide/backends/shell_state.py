"""GNOME Shell's saved brightness, set before the shell starts.

At login GNOME Shell (49 and later, misc/brightnessManager.js) sets the
panel to the scale it saved in the last session, before anyone can send it
an automatic target. Under Luxtide that scale is an offset around our target
and has nothing to do with the brightness on screen: 55 % at night on a
laptop whose night level is 15 %, until our target lands. Starting before
the shell (the unit is ordered before graphical-session-pre.target), the
daemon writes the level it is about to set into that saved state, so the
shell comes up where Luxtide will keep it and nothing flashes.

The state is $XDG_DATA_HOME/gnome-shell/brightnesses, the shell's persistent
state "brightnesses": a serialised GVariant a(sssssd) of vendor, product,
serial, colour mode, connector and scale per monitor. It is written only
while no gnome-shell of this user runs: the shell reads it at start and on
monitor changes, and overwrites it with its own values after every change
by the user, so a write behind a running shell would surface later as a
jump. Entries are only ever changed, never added: the monitor's identity is
Mutter's to give.

While the shell runs, the file is the shell's own word on its scale, saved
300 ms after each change by the user; backlight.GnomeShellMethod reads it
to catch a press it could not see on the panel (saved_scale).

The scale written is capped at one half. The shell shows
clamp(target + scale - 0.5); with the scale at or below one half every level
down to the bottom stays reachable, while a target above 1 still reaches the
top. At night the cap does not matter; a boot by day starts at 50 % for the
moment before our target arrives.
"""

import logging
import os
import pathlib

log = logging.getLogger(__name__)

STATE_TYPE = "a(sssssd)"
SHELL_COMM = "gnome-shell"
MAX_START_SCALE = 0.5


def state_path():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return pathlib.Path(base) / "gnome-shell" / "brightnesses"


def start_scale(level):
    """The saved scale that makes the shell start at `level` percent."""
    return min(max(level, 0) / 100.0, MAX_START_SCALE)


def shell_running(proc="/proc", uid=None):
    """Is a gnome-shell of this user running? (Not the login screen's.)"""
    uid = os.getuid() if uid is None else uid
    try:
        entries = os.scandir(proc)
    except OSError:
        return False
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                with open(os.path.join(entry.path, "comm"), encoding="utf-8") as handle:
                    if handle.read().strip() == SHELL_COMM:
                        return True
            except OSError:
                continue                    # gone meanwhile
    return False


def read(path):
    """The saved entries as tuples, or None when the file is missing or not
    what we expect (then it is left alone)."""
    from gi.repository import GLib
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError:
        return None
    if not data:
        return None
    variant = GLib.Variant.new_from_bytes(GLib.VariantType(STATE_TYPE),
                                          GLib.Bytes.new(data), False)
    # Anything that does not serialise back to the same bytes was not
    # written as a(sssssd).
    if variant.get_data_as_bytes().get_data() != data:
        return None
    entries = variant.unpack()
    if not all(0.0 <= entry[5] <= 1.0 for entry in entries):
        return None
    return entries


def saved_scale(connector, path=None):
    """The scale the shell saved last for `connector` ("eDP-1"), or None.

    The shell saves every monitor it knows, removing the monitor's old entry
    and appending the new one, so of several entries for one connector (a
    replaced panel, another colour mode) the last is the current one."""
    try:
        entries = read(path or state_path())
    except Exception as error:              # a GLib surprise: no answer
        log.debug("could not read the shell's saved brightness: %s", error)
        return None
    values = [entry[5] for entry in entries or () if entry[4] == connector]
    return values[-1] if values else None


def write(path, entries):
    from gi.repository import GLib
    path = pathlib.Path(path)
    data = GLib.Variant(STATE_TYPE, entries).get_data_as_bytes().get_data()
    tmp = path.with_name(f".{path.name}.luxtide-tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, path)


def preset(connector, level, path=None, proc="/proc"):
    """Make the shell start at `level` percent on `connector` ("eDP-1").
    Returns the scale it will start with, or None when nothing was done."""
    path = path or state_path()
    if shell_running(proc):
        return None
    try:
        entries = read(path)
    except Exception as error:              # a GLib surprise: leave the file alone
        log.debug("could not read %s: %s", path, error)
        return None
    if not entries:
        return None
    scale = start_scale(level)
    ours = [i for i, entry in enumerate(entries) if entry[4] == connector]
    if not ours:
        return None
    if all(abs(entries[i][5] - scale) < 1e-6 for i in ours):
        return scale
    updated = [(*entry[:5], scale) if i in ours else tuple(entry)
               for i, entry in enumerate(entries)]
    try:
        write(path, updated)
    except OSError as error:
        log.warning("could not preset GNOME's saved brightness: %s", error)
        return None
    return scale
