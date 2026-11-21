"""Where things live, following the XDG base directory spec.

Functions rather than constants, so tests can point the environment
somewhere else before calling them.
"""

import logging
import os
import pathlib
import shutil

from . import APP

LEGACY_APP = "ddc-sun-dimmer"           # the project's former name


def _xdg(variable, fallback):
    value = os.environ.get(variable)
    return pathlib.Path(value) if value else pathlib.Path.home() / fallback


def config_file():
    return _xdg("XDG_CONFIG_HOME", ".config") / APP / "config.ini"


def cache_dir():
    """Disposable data: the weather forecast."""
    return _xdg("XDG_CACHE_HOME", ".cache") / APP


def state_dir():
    """Small preferences that should survive a reboot (last pause length)."""
    return _xdg("XDG_STATE_HOME", ".local/state") / APP


def _runtime_base():
    base = os.environ.get("XDG_RUNTIME_DIR")
    return pathlib.Path(base) if base else None


def runtime_dir():
    """Volatile state: pauses and overrides.

    It lives on tmpfs and disappears at reboot, which is what we want. After a
    power cycle the monitor no longer holds what we last wrote, so any memory
    of the hardware state would be a lie.
    """
    return _named_runtime_dir(APP)


def _named_runtime_dir(name):
    base = _runtime_base()
    if base:
        return base / name
    return pathlib.Path("/tmp") / f"{name}-{os.getuid()}"


def migrate_legacy():
    """Copy settings and state left by ddc-sun-dimmer, once.

    Each directory is copied only while the new one does not exist yet, so
    this is cheap to call at every start and never overwrites anything. The
    old directories stay in place, in case of a way back.
    """
    pairs = [
        (config_file().parent, _xdg("XDG_CONFIG_HOME", ".config") / LEGACY_APP),
        (cache_dir(), _xdg("XDG_CACHE_HOME", ".cache") / LEGACY_APP),
        (state_dir(), _xdg("XDG_STATE_HOME", ".local/state") / LEGACY_APP),
        (runtime_dir(), _named_runtime_dir(LEGACY_APP)),
    ]
    for new, old in pairs:
        if new.exists() or not old.is_dir():
            continue
        try:
            shutil.copytree(old, new, symlinks=True)
            logging.getLogger(__name__).info("copied %s to %s", old, new)
        except OSError as error:
            logging.getLogger(__name__).warning("could not copy %s: %s", old, error)
            continue
        if new == config_file().parent:
            _retire_mutter_method(config_file())


def _retire_mutter_method(path):
    """ddc-sun-dimmer wrote the backlight through Mutter on GNOME. On GNOME 49
    and later that leaves the shell's brightness keys jumping from a stale
    level; the shell's own interface replaced it, and "auto" picks that. A
    copied setting of "mutter" becomes "auto"; one chosen later stays."""
    try:
        text = path.read_text()
    except OSError:
        return
    lines = text.split("\n")
    section = ""
    for number, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
        elif section == "[backlight]" and stripped.replace(" ", "") == "method=mutter":
            lines[number] = "method = auto"
    if lines != text.split("\n"):
        atomic_write(path, "\n".join(lines))


def atomic_write(path, text):
    """Write a file so that readers never see it half-written."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
