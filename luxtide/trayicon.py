"""The brightness level drawn as the tray icon itself.

StatusNotifierItem has no text of its own. GNOME's AppIndicator extension and
the Ayatana indicator applets read the XAyatanaLabel extension and show it
next to the icon; every other host (Plasma, XFCE, Cinnamon, LXQt, Budgie,
waybar…) shows the icon alone. There the number becomes the icon: a pixmap
drawn with cairo, ARGB32 in network byte order as the specification asks.

The text colour has to match the panel, which the host never tells us.
Plasma's comes from its theme and colour scheme; anywhere else we draw white
with a dark outline, readable on light and dark panels alike.
"""

import array
import functools
import logging
import os
import pathlib
import sys

log = logging.getLogger(__name__)

SIZES = (16, 22, 24, 32, 48, 64)
WHITE = (252, 252, 252)
DARK = (35, 38, 41)

# Watchers whose hosts show XAyatanaLabel, by process name (/proc/PID/comm,
# at most 15 characters).
LABEL_HOSTS = ("gnome-shell", "ayatana-indica", "indicator-appli")


def label_host(process):
    """Does the tray host owned by this process show text next to icons?"""
    return bool(process) and process.startswith(LABEL_HOSTS)


def available():
    try:
        import cairo  # noqa: F401
    except ImportError:
        return False
    return True


# --- colour ------------------------------------------------------------------


def _config_home():
    return pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"))


def read_kde_value(path, group, key):
    """One value from a KDE ini file, or None. KDE's syntax (immutable
    markers, duplicate keys) trips configparser, so read it by hand."""
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    current = None
    for line in lines:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]           # "Colors:Header][Inactive" stays apart
        elif current == group and "=" in line:
            name, value = line.split("=", 1)
            if name.split("[", 1)[0].strip() == key:
                return value.strip()
    return None


def _rgb(text):
    try:
        parts = [int(p) for p in text.split(",")[:3]]
    except (AttributeError, ValueError):
        return None
    if len(parts) != 3 or not all(0 <= p <= 255 for p in parts):
        return None
    return tuple(parts)


def plasma_colours(config_home=None):
    """(text colour, outline) for Plasma's panel. The Breeze light and dark
    themes have fixed colours; the default one follows the colour scheme,
    whose Header colours the panel uses. Other themes may differ from the
    scheme, so they get an outline as well."""
    base = pathlib.Path(config_home) if config_home else _config_home()
    theme = (read_kde_value(base / "plasmarc", "Theme", "name") or "default").lower()
    if theme == "breeze-dark":
        return WHITE, False
    if theme == "breeze-light":
        return DARK, False
    colour = None
    for group in ("Colors:Header", "Colors:Window"):
        colour = _rgb(read_kde_value(base / "kdeglobals", group, "ForegroundNormal"))
        if colour:
            break
    if colour is None:
        return WHITE, True
    return colour, theme != "default"


def colours(process, config_home=None):
    """(text colour, outline) for the tray host run by this process."""
    if process and (process.startswith("kded") or process.startswith("plasmashell")):
        return plasma_colours(config_home)
    return WHITE, True


def watched_files(process, config_home=None):
    """Files whose change may recolour the icon."""
    if process and (process.startswith("kded") or process.startswith("plasmashell")):
        base = pathlib.Path(config_home) if config_home else _config_home()
        return [base / "kdeglobals", base / "plasmarc"]
    return []


# --- drawing -----------------------------------------------------------------


def _opposite(colour):
    luma = 0.2126 * colour[0] + 0.7152 * colour[1] + 0.0722 * colour[2]
    return (0, 0, 0) if luma > 128 else (255, 255, 255)


def render(text, size, colour, outline):
    """One size: (width, height, ARGB32 bytes in network byte order)."""
    import cairo

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    options = cairo.FontOptions()
    options.set_antialias(cairo.ANTIALIAS_GRAY)
    options.set_hint_style(cairo.HINT_STYLE_SLIGHT)
    cr.set_font_options(options)
    cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    line = max(1.0, size / 12.0) if outline else 0.0
    # Two digits fill the height a panel gives its icons; three, the width.
    cr.set_font_size(size)
    x_bearing, y_bearing, width, height, _, _ = cr.text_extents(text)
    room = size - 2 * line - 2 * max(1.0, size / 16.0)
    scale = min(room / width, room * 0.8 / height) if width and height else 1.0
    cr.set_font_size(size * scale)
    x_bearing, y_bearing, width, height, _, _ = cr.text_extents(text)
    cr.move_to((size - width) / 2 - x_bearing, (size - height) / 2 - y_bearing)
    cr.text_path(text)
    if outline:
        r, g, b = _opposite(colour)
        cr.set_source_rgba(r / 255, g / 255, b / 255, 0.7)
        cr.set_line_width(2 * line)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke_preserve()
    cr.set_source_rgb(*(c / 255 for c in colour))
    cr.fill()
    surface.flush()
    return size, size, _network_argb(surface, size)


def _network_argb(surface, size):
    """cairo keeps premultiplied ARGB in native-endian words; the
    specification wants plain ARGB, most significant byte first."""
    stride = surface.get_stride()
    data = bytes(surface.get_data())
    out = bytearray(size * size * 4)
    at = 0
    for row in range(size):
        words = array.array("I", data[row * stride:row * stride + size * 4])
        if sys.byteorder == "big":
            words.byteswap()
        for word in words:
            a = word >> 24
            if a:
                r, g, b = (word >> 16) & 255, (word >> 8) & 255, word & 255
                if a < 255:
                    half = a // 2
                    r = min(255, (r * 255 + half) // a)
                    g = min(255, (g * 255 + half) // a)
                    b = min(255, (b * 255 + half) // a)
                out[at:at + 4] = bytes((a, r, g, b))
            at += 4
    return bytes(out)


@functools.lru_cache(maxsize=8)
def pixmaps(text, colour, outline):
    """Every size, for the a(iiay) IconPixmap property; [] without cairo."""
    try:
        return [render(text, size, colour, outline) for size in SIZES]
    except ImportError:
        return []
    except Exception as failure:     # a broken font setup must not stop the daemon
        log.warning("could not draw the level into the tray icon: %s", failure)
        return []
