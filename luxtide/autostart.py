"""Start at login: one switch for every desktop, with or without systemd.

The switch is an XDG autostart entry, which GNOME, Plasma, XFCE, Cinnamon,
MATE and LXQt all read, with or without systemd, and show in their own
startup settings. A package installs the entry in /etc/xdg/autostart, on for
every user. Turning it off writes a same-named entry with Hidden=true to
~/.config/autostart, which the specification defines as "deleted" and which
is what the desktops' own settings do. Without a system entry (`make
install-user`) the user entry itself is the switch.

The entry runs `luxtide autostart`: under a systemd user manager that
starts the user unit, keeping journal logging and restart on failure;
elsewhere it runs the daemon in place.

Under systemd the switch also enables the unit, which then starts with the
session's early services (graphical-session-pre.target), before the desktop
shell, instead of with the autostart entries after it. The level is then set
the moment the shell is up rather than seconds later, which at night means no
flash of the brightness the desktop restores. The entry stays as the way in
for a package install, where nothing is enabled yet: its first run enables
the unit for the next login.

Compositors without autostart support (sway, Hyprland, niri) run
`luxtide autostart` from their own configuration, or enable the unit.
An enabled unit counts as "on" here, and turning the switch off disables it,
so that "off" means off.
"""

import configparser
import os
import pathlib
import shutil
import subprocess
import sys

from . import APP, paths

ENTRY_NAME = "io.github.MikolajQ.Luxtide.desktop"
UNIT = f"{APP}.service"
EARLY_TARGET = "graphical-session-pre.target"
LEGACY_UNIT = "ddc-sun-dimmer.service"

ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Luxtide
Comment=Screen brightness that follows the sun
Exec={exec} autostart
Icon=display-brightness-symbolic
Terminal=false
X-GNOME-Autostart-enabled={enabled}
{hidden}"""


def user_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return pathlib.Path(base) / "autostart"


def system_dirs():
    dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    return [pathlib.Path(d) / "autostart" for d in dirs.split(":") if d]


def user_entry():
    return user_dir() / ENTRY_NAME


def system_entry():
    for directory in system_dirs():
        path = directory / ENTRY_NAME
        if path.is_file():
            return path
    return None


def read_entry(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
        return dict(parser["Desktop Entry"])
    except (configparser.Error, KeyError, OSError, UnicodeDecodeError):
        return {}


def entry_active(values):
    return (values.get("Hidden", "false").strip().lower() != "true"
            and values.get("X-GNOME-Autostart-enabled", "true").strip().lower() != "false")


def unit_links():
    """Symlinks that enable the systemd user unit for this user."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return sorted((pathlib.Path(base) / "systemd" / "user").glob(f"*.wants/{UNIT}"))


def entry_enabled():
    """Does the XDG entry start us at login?"""
    path = user_entry()
    if path.is_file():
        return entry_active(read_entry(path))
    path = system_entry()
    return path is not None and entry_active(read_entry(path))


def is_enabled():
    return entry_enabled() or bool(unit_links())


def executable():
    """How the entry should call us: the path we were started by, kept
    unresolved so that a ~/.local/bin symlink stays a symlink."""
    here = pathlib.Path(os.path.abspath(sys.argv[0])).parent
    candidate = here / APP
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(APP) or APP


def quote_exec(path):
    if any(c in path for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") \
            .replace("$", "\\$") + '"'
    return path


def entry_text(enabled, exec_path=None):
    return ENTRY_TEMPLATE.format(exec=quote_exec(exec_path or executable()),
                                 enabled="true" if enabled else "false",
                                 hidden="" if enabled else "Hidden=true\n")


def set_enabled(enabled, exec_path=None):
    """Turn starting at login on or off. Raises OSError on failure."""
    user, system = user_entry(), system_entry()
    system_on = system is not None and entry_active(read_entry(system))
    if enabled:
        if system_on:
            if user.is_file() or user.is_symlink():
                user.unlink()                   # drop our override
        else:
            if user.is_symlink():
                user.unlink()
            paths.atomic_write(user, entry_text(True, exec_path))
        if systemd_unit_available():
            enable_unit()
    else:
        if system is not None:
            if user.is_symlink():
                user.unlink()
            paths.atomic_write(user, entry_text(False, exec_path))
        elif user.is_file() or user.is_symlink():
            user.unlink()
        if unit_links():
            disable_unit()


def unit_enabled_early():
    return any(link.parent.name == f"{EARLY_TARGET}.wants" for link in unit_links())


def enable_unit():
    """Enable the user unit for early start. A unit enabled by an older
    version, under graphical-session.target, is moved."""
    verb = "reenable" if unit_links() else "enable"
    result = subprocess.run(["systemctl", "--user", verb, UNIT],
                            capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"systemctl --user {verb} failed")


def remove_legacy_links():
    """The ddc-sun-dimmer package's unit, enabled per user, leaves a dangling
    link once the luxtide package replaces it."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    for link in (pathlib.Path(base) / "systemd" / "user").glob(f"*.wants/{LEGACY_UNIT}"):
        if link.is_symlink() and not link.exists():
            link.unlink()


def adopt_unit():
    """Run by the login entry: when starting at login is on, make sure the
    unit is enabled for early start next time. Quiet; True when it changed
    anything."""
    remove_legacy_links()
    if unit_enabled_early() or not entry_enabled() or not systemd_unit_available():
        return False
    try:
        enable_unit()
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def disable_unit():
    if not shutil.which("systemctl"):
        for link in unit_links():
            link.unlink()
        return
    result = subprocess.run(["systemctl", "--user", "disable", UNIT],
                            capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "systemctl --user disable failed")


def systemd_unit_available():
    """Is there a running systemd user manager that knows our unit?"""
    if not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(["systemctl", "--user", "show", "--property=LoadState",
                                 "--value", UNIT], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "loaded"


def start_unit():
    """Ask systemd to start the daemon; False when that is not possible."""
    if not systemd_unit_available():
        return False
    try:
        result = subprocess.run(["systemctl", "--user", "start", "--no-block", UNIT],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
