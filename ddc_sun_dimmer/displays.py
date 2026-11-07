"""Finding displays through sysfs: no ddcutil probing, no root.

Every DRM connector says whether something is plugged in (`status`),
whether its panel is lit (`dpms`), and on most drivers which I2C bus
carries its DDC channel (the `ddc` link). That last link makes `ddcutil
detect` unnecessary and ties each bus to the right power state, which
matters as soon as there is more than one monitor.
"""

import dataclasses
import os
import pathlib
import re

DRM_ROOT = pathlib.Path("/sys/class/drm")
BACKLIGHT_ROOT = pathlib.Path("/sys/class/backlight")
INTERNAL_PREFIXES = ("eDP", "LVDS", "DSI")
BACKLIGHT_TYPE_ORDER = {"firmware": 0, "platform": 1, "raw": 2}


def _read(path, default=""):
    try:
        return pathlib.Path(path).read_text().strip()
    except OSError:
        return default


def parse_edid(data):
    """(manufacturer, model name, serial text) from an EDID block, or Nones."""
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None, None, None
    code = (data[8] << 8) | data[9]
    manufacturer = "".join(chr(((code >> shift) & 0x1F) + 64) for shift in (10, 5, 0))
    model = serial = None
    for offset in (54, 72, 90, 108):
        block = data[offset:offset + 18]
        if block[0:2] != b"\x00\x00" or block[2] != 0:
            continue
        text = block[5:18].split(b"\x0a")[0].decode("ascii", "replace").strip()
        if block[3] == 0xFC and text:
            model = text
        elif block[3] == 0xFF and text:
            serial = text
    if model is None:
        model = f"{(data[11] << 8) | data[10]:04X}"
    return manufacturer, model, serial


def sanitize_key(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "display"


@dataclasses.dataclass
class Connector:
    name: str                  # "card1-DVI-D-1"
    path: pathlib.Path
    connected: bool
    bus: int | None            # I2C bus of the DDC channel
    manufacturer: str | None = None
    model: str | None = None
    serial: str | None = None
    key: str = ""

    @property
    def short(self):
        """Connector name as compositors print it: "DVI-D-1"."""
        return self.name.split("-", 1)[1] if "-" in self.name else self.name

    @property
    def internal(self):
        return self.short.startswith(INTERNAL_PREFIXES)

    @property
    def label(self):
        if self.manufacturer and self.model:
            return f"{self.manufacturer} {self.model} ({self.short})"
        return self.short

    def awake(self):
        """Is the panel lit? A sleeping monitor does not answer on DDC."""
        state = _read(self.path / "dpms", "On")
        return state == "On"


def list_connectors(root=DRM_ROOT):
    connectors = []
    for path in sorted(pathlib.Path(root).glob("card*-*")):
        if not (path / "status").exists():
            continue
        connected = _read(path / "status") == "connected"
        bus = None
        ddc = path / "ddc"
        if ddc.exists():
            match = re.search(r"i2c-(\d+)$", os.path.realpath(ddc))
            if match:
                bus = int(match.group(1))
        manufacturer = model = serial = None
        if connected:
            try:
                manufacturer, model, serial = parse_edid((path / "edid").read_bytes())
            except OSError:
                pass
        connectors.append(Connector(path.name, path, connected, bus,
                                    manufacturer, model, serial))
    _assign_keys(connectors)
    return connectors


def _assign_keys(connectors):
    """Stable names for settings: the monitor model, not the port it sits in."""
    counts = {}
    for c in connectors:
        if c.internal:
            c.key = "internal"
        elif c.manufacturer and c.model:
            c.key = sanitize_key(f"{c.manufacturer}-{c.model}")
        else:
            c.key = sanitize_key(c.short)
        counts[c.key] = counts.get(c.key, 0) + 1
    # Two identical monitors: tell them apart by serial, else by port.
    for c in connectors:
        if counts[c.key] > 1:
            c.key = sanitize_key(f"{c.key}-{c.serial or c.short}")


@dataclasses.dataclass
class BacklightDevice:
    name: str                  # "intel_backlight"
    path: pathlib.Path
    type: str                  # firmware, platform or raw
    max_brightness: int
    connector: Connector | None = None

    def read_raw(self):
        for attribute in ("actual_brightness", "brightness"):
            value = _read(self.path / attribute)
            if value.isdigit():
                return int(value)
        return None

    def awake(self):
        return self.connector.awake() if self.connector else True


def list_backlights(root=BACKLIGHT_ROOT, connectors=None):
    connectors = connectors if connectors is not None else list_connectors()
    internal = [c for c in connectors if c.internal and c.connected]
    devices = []
    for path in sorted(pathlib.Path(root).glob("*")):
        maximum = _read(path / "max_brightness")
        if not maximum.isdigit() or int(maximum) < 1:
            continue
        kind = _read(path / "type", "raw")
        # i915 nests the backlight inside its connector; otherwise assume the
        # single built-in panel.
        owner = next((c for c in connectors if (c.path / path.name).exists()), None)
        if owner is None and len(internal) == 1:
            owner = internal[0]
        devices.append(BacklightDevice(path.name, path, kind, int(maximum), owner))
    return devices


def pick_backlight(devices, preferred=""):
    """The device to drive, the way desktops choose it.

    Firmware interfaces know the panel's real range, platform drivers come
    next, and raw GPU registers last, because on hybrid laptops a raw device
    may belong to a GPU that is not driving the panel.
    """
    if preferred:
        return next((d for d in devices if d.name == preferred), None)
    if not devices:
        return None
    return min(devices, key=lambda d: (d.connector is None,
                                       BACKLIGHT_TYPE_ORDER.get(d.type, 3), d.name))
