"""Small interface preferences shared by the tray and the settings window."""

import json

from . import paths


def _file():
    return paths.state_dir() / "ui.json"


def load_ui_prefs():
    try:
        data = json.loads(_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ui_prefs(prefs):
    paths.atomic_write(_file(), json.dumps(prefs))


def remember(key, value):
    prefs = load_ui_prefs()
    prefs[key] = value
    save_ui_prefs(prefs)
