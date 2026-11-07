"""Where things live, following the XDG base directory spec.

Functions rather than constants, so tests can point the environment
somewhere else before calling them.
"""

import os
import pathlib

from . import APP


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


def runtime_dir():
    """Volatile state: pauses and overrides.

    It lives on tmpfs and disappears at reboot, which is what we want. After a
    power cycle the monitor no longer holds what we last wrote, so any memory
    of the hardware state would be a lie.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return pathlib.Path(base) / APP
    return pathlib.Path("/tmp") / f"{APP}-{os.getuid()}"


def atomic_write(path, text):
    """Write a file so that readers never see it half-written."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
