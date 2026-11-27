"""The settings file: parsed with precise errors, written back documented.

One INI file, shared by the daemon (which reloads it as soon as it changes),
the command line and the settings window (which rewrites it in a canonical,
commented form).
"""

import configparser
import dataclasses
import pathlib

from . import MAX_LEVEL, MIN_LEVEL, paths, solar


class ConfigError(ValueError):
    """A setting is missing, malformed or inconsistent."""


@dataclasses.dataclass
class DdcSettings:
    """How to talk to DDC/CI monitors. Per-display sections may override."""

    enabled: bool = True
    bus: str = ""                      # restrict to one I2C bus, e.g. "5"
    sleep_multiplier: float = 1.0
    retries: int = 3                   # attempts per transaction on visible errors
    repeats: int = 1                   # identical writes, against silently lost frames
    settle: float = 0.2                # seconds between repeats
    syslog: str = "NEVER"              # ddcutil --syslog level; empty = omit flag
    mccs: str = ""                     # MCCS version to skip detection, e.g. "2.0"
    skip_ddc_checks: bool = True
    wake_burst: bool = False           # re-assert repeatedly in the first minute after wake
    refresh_minutes: int = 0           # periodic re-write while dimmed; 0 = never
    restore_level: int = 100           # what the monitor falls back to by itself


# Ways to drive a laptop panel; see backends/backlight.py.
METHODS = ("auto", "gnome-shell", "mutter", "kde", "logind", "sysfs")

# Settings that a [display:...] section may override.
DDC_OVERRIDABLE = ("sleep_multiplier", "retries", "repeats", "settle", "syslog",
                   "mccs", "skip_ddc_checks", "wake_burst", "refresh_minutes",
                   "restore_level")


@dataclasses.dataclass
class DisplaySettings:
    enabled: bool = True
    offset: int = 0                    # added to the common level, in percent points
    minimum: int = MIN_LEVEL
    maximum: int = MAX_LEVEL
    ddc: dict = dataclasses.field(default_factory=dict)

    def is_default(self):
        return self == DisplaySettings()


def _default_presets():
    return [15, 30, 60, 120]


@dataclasses.dataclass
class Config:
    # [location]
    latitude: float | None = None
    longitude: float | None = None
    timezone: str = ""
    location_follow: bool = False      # let the daemon update it from GeoClue
    location_method: str = ""          # how it was found: manual, geoclue, search, map
    location_place: str = ""           # the place name, when found by search
    location_accuracy: int = 0         # metres; 0 = unknown
    location_updated: str = ""         # "YYYY-MM-DD HH:MM"
    # [levels]
    day: int = 100
    twilight: int = 75
    night: int = 1
    step: int = 20                     # % of the level per key press or menu step
    # [schedule]
    mode: str = "time"                 # "time" or "elevation"
    smooth: bool = True
    margin_minutes: int = 60
    curve_shape: float = 1.0           # 0 linear .. 1 geometric
    night_elevation: float = -6.0
    twilight_elevation: float = 0.0
    day_elevation: float = 6.0
    # [weather]
    weather_enabled: bool = False
    weather_strength: int = 40         # % less light at 100 % cloud cover
    weather_refresh_minutes: int = 60
    weather_max_age_hours: int = 12
    # [ddc]
    ddc: DdcSettings = dataclasses.field(default_factory=DdcSettings)
    # [backlight]
    backlight_enabled: bool = True
    backlight_device: str = ""
    backlight_method: str = "auto"     # auto, gnome-shell, mutter, kde, logind, sysfs
    backlight_follow_manual: bool = True
    # [tray]
    tray_enabled: bool = True
    tray_show_level: bool = False
    tray_pause_presets: list = dataclasses.field(default_factory=_default_presets)
    # [display:KEY]
    displays: dict = dataclasses.field(default_factory=dict)

    source: pathlib.Path | None = dataclasses.field(default=None, compare=False)

    # --- derived values ---------------------------------------------------

    def tz(self):
        return solar.load_timezone(self.timezone or None)

    def zone_name(self):
        return self.timezone or solar.system_timezone_name() or "UTC"

    def set_location(self, latitude, longitude, method, place="", accuracy=0):
        """Coordinates found one way or another, with a note of how."""
        from .locate import now_text
        self.latitude, self.longitude = round(latitude, 4), round(longitude, 4)
        self.location_method = method
        self.location_place = place
        self.location_accuracy = int(round(accuracy or 0))
        self.location_updated = now_text()

    def location(self):
        """(latitude, longitude, source) where source is 'config' or 'timezone'."""
        if self.latitude is not None and self.longitude is not None:
            return self.latitude, self.longitude, "config"
        coords = solar.zone_coordinates(self.zone_name())
        if coords:
            return coords[0], coords[1], "timezone"
        raise ConfigError(
            "no location: set latitude and longitude in [location] "
            f"(time zone {self.zone_name()!r} has no reference city)")

    def display(self, key):
        return self.displays.get(key) or DisplaySettings()

    def ddc_for(self, key):
        """DDC settings for one display: global defaults plus its overrides."""
        return dataclasses.replace(self.ddc, **self.display(key).ddc)

    def validate(self):
        """Checks that span several settings. Raises ConfigError."""
        if (self.latitude is None) != (self.longitude is None):
            raise ConfigError("[location] latitude and longitude must be set together")
        if not (self.night_elevation < self.twilight_elevation < self.day_elevation):
            raise ConfigError(
                "[schedule] needs night_elevation < twilight_elevation < day_elevation")
        if self.timezone:
            try:
                solar.load_timezone(self.timezone)
            except Exception as error:
                raise ConfigError(f"[location] timezone {self.timezone!r}: {error}") from None
        for key, display in self.displays.items():
            if display.minimum > display.maximum:
                raise ConfigError(f"[display:{key}] minimum is above maximum")
        self.location()


# --- reading ---------------------------------------------------------------


class _Reader:
    """Typed access to the parser with errors that name the offending line."""

    def __init__(self, parser, origin):
        self.parser = parser
        self.origin = origin

    def has(self, section, key):
        return self.parser.has_option(section, key) and \
            self.parser.get(section, key).strip() != ""

    def raw(self, section, key):
        return self.parser.get(section, key).strip()

    def fail(self, section, key, expected):
        value = self.raw(section, key)
        raise ConfigError(f"{self.origin}: [{section}] {key} = {value!r}: {expected}")

    def text(self, section, key, default):
        return self.raw(section, key) if self.parser.has_option(section, key) else default

    def integer(self, section, key, default, low, high):
        if not self.has(section, key):
            return default
        try:
            value = int(self.raw(section, key))
        except ValueError:
            self.fail(section, key, f"expected a whole number from {low} to {high}")
        if not low <= value <= high:
            self.fail(section, key, f"expected a whole number from {low} to {high}")
        return value

    def number(self, section, key, default, low, high):
        if not self.has(section, key):
            return default
        try:
            value = float(self.raw(section, key))
        except ValueError:
            self.fail(section, key, f"expected a number from {low:g} to {high:g}")
        if not low <= value <= high:
            self.fail(section, key, f"expected a number from {low:g} to {high:g}")
        return value

    def boolean(self, section, key, default):
        if not self.has(section, key):
            return default
        value = self.raw(section, key).lower()
        if value in configparser.ConfigParser.BOOLEAN_STATES:
            return configparser.ConfigParser.BOOLEAN_STATES[value]
        self.fail(section, key, "expected yes or no")

    def choice(self, section, key, default, choices):
        if not self.has(section, key):
            return default
        value = self.raw(section, key).lower()
        if value not in choices:
            self.fail(section, key, "expected one of: " + ", ".join(choices))
        return value

    def optional_number(self, section, key, low, high):
        return self.number(section, key, None, low, high) if self.has(section, key) else None


def _read_ddc(reader, section, base, keys):
    """Parse DDC settings from `section`; returns {name: value} found there."""
    found = {}
    spec = {
        "sleep_multiplier": lambda d: reader.number(section, "sleep_multiplier", d, 0.1, 10),
        "retries": lambda d: reader.integer(section, "retries", d, 1, 20),
        "repeats": lambda d: reader.integer(section, "repeats", d, 1, 10),
        "settle": lambda d: reader.number(section, "settle", d, 0, 5),
        "syslog": lambda d: reader.text(section, "syslog", d),
        "mccs": lambda d: reader.text(section, "mccs", d),
        "skip_ddc_checks": lambda d: reader.boolean(section, "skip_ddc_checks", d),
        "wake_burst": lambda d: reader.boolean(section, "wake_burst", d),
        "refresh_minutes": lambda d: reader.integer(section, "refresh_minutes", d, 0, 1440),
        "restore_level": lambda d: reader.integer(section, "restore_level", d,
                                                  MIN_LEVEL, MAX_LEVEL),
    }
    for name in keys:
        if reader.parser.has_option(section, name):
            found[name] = spec[name](getattr(base, name))
    return found


def parse(text, origin="config.ini"):
    """Parse INI text into a Config. Raises ConfigError."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text, source=str(origin))
    except configparser.Error as error:
        raise ConfigError(f"{origin}: {error}") from None
    for section in ("location", "levels", "schedule", "weather", "ddc",
                    "backlight", "tray", "legacy"):
        if not parser.has_section(section):
            parser.add_section(section)
    r = _Reader(parser, origin)
    cfg = Config()

    cfg.latitude = r.optional_number("location", "latitude", -90, 90)
    cfg.longitude = r.optional_number("location", "longitude", -180, 180)
    cfg.timezone = r.text("location", "timezone", "")
    cfg.location_follow = r.boolean("location", "follow", cfg.location_follow)
    cfg.location_method = r.choice("location", "method", "",
                                   ("", "manual", "geoclue", "search", "map"))
    cfg.location_place = r.text("location", "place", "")
    cfg.location_accuracy = r.integer("location", "accuracy", 0, 0, 20_000_000)
    cfg.location_updated = r.text("location", "updated", "")

    cfg.day = r.integer("levels", "day", cfg.day, MIN_LEVEL, MAX_LEVEL)
    cfg.twilight = r.integer("levels", "twilight", cfg.twilight, MIN_LEVEL, MAX_LEVEL)
    cfg.night = r.integer("levels", "night", cfg.night, MIN_LEVEL, MAX_LEVEL)
    cfg.step = r.integer("levels", "step", cfg.step, 1, 50)

    cfg.mode = r.choice("schedule", "mode", cfg.mode, ("time", "elevation"))
    cfg.smooth = r.boolean("schedule", "smooth", cfg.smooth)
    cfg.margin_minutes = r.integer("schedule", "margin_minutes", cfg.margin_minutes, 1, 360)
    if r.has("schedule", "curve_shape"):
        cfg.curve_shape = r.number("schedule", "curve_shape", 1.0, 0, 1)
    else:
        # Version 1 had a two-way switch instead of a continuous shape.
        legacy = r.choice("schedule", "curve", "perceptual", ("perceptual", "linear"))
        cfg.curve_shape = 1.0 if legacy == "perceptual" else 0.0
    cfg.night_elevation = r.number("schedule", "night_elevation", cfg.night_elevation, -18, 30)
    cfg.twilight_elevation = r.number("schedule", "twilight_elevation",
                                      cfg.twilight_elevation, -18, 30)
    cfg.day_elevation = r.number("schedule", "day_elevation", cfg.day_elevation, -18, 30)

    cfg.weather_enabled = r.boolean("weather", "enabled", cfg.weather_enabled)
    cfg.weather_strength = r.integer("weather", "strength", cfg.weather_strength, 0, 90)
    cfg.weather_refresh_minutes = r.integer("weather", "refresh_minutes",
                                            cfg.weather_refresh_minutes, 15, 1440)
    cfg.weather_max_age_hours = r.integer("weather", "max_age_hours",
                                          cfg.weather_max_age_hours, 1, 72)

    ddc = DdcSettings()
    ddc.enabled = r.boolean("ddc", "enabled", ddc.enabled)
    ddc.bus = r.text("ddc", "bus", ddc.bus)
    for name, value in _read_ddc(r, "ddc", ddc, DDC_OVERRIDABLE).items():
        setattr(ddc, name, value)
    # Version 1 kept these in [schedule] and always ran the wake-up burst.
    if not parser.has_option("ddc", "refresh_minutes") and r.has("schedule", "refresh_minutes"):
        ddc.refresh_minutes = r.integer("schedule", "refresh_minutes", 0, 0, 1440)
    if not parser.has_option("ddc", "wake_burst") and \
            parser.has_option("schedule", "wake_window_seconds"):
        ddc.wake_burst = True
    cfg.ddc = ddc

    cfg.backlight_enabled = r.boolean("backlight", "enabled", cfg.backlight_enabled)
    cfg.backlight_device = r.text("backlight", "device", cfg.backlight_device)
    cfg.backlight_method = r.choice("backlight", "method", cfg.backlight_method,
                                    METHODS)
    cfg.backlight_follow_manual = r.boolean("backlight", "follow_manual",
                                            cfg.backlight_follow_manual)

    cfg.tray_enabled = r.boolean("tray", "enabled", cfg.tray_enabled)
    cfg.tray_show_level = r.boolean("tray", "show_level", cfg.tray_show_level)
    if r.has("tray", "pause_presets"):
        presets = []
        for item in r.raw("tray", "pause_presets").replace(";", ",").split(","):
            item = item.strip()
            if not item:
                continue
            if not item.isdigit() or not 1 <= int(item) <= 1440:
                r.fail("tray", "pause_presets", "expected minutes from 1 to 1440, "
                       "separated by commas")
            presets.append(int(item))
        cfg.tray_pause_presets = sorted(set(presets))[:8] or _default_presets()

    for section in parser.sections():
        if not section.startswith("display:"):
            continue
        key = section.split(":", 1)[1].strip()
        if not key:
            raise ConfigError(f"{origin}: [{section}] needs a display name")
        display = DisplaySettings()
        display.enabled = r.boolean(section, "enabled", True)
        display.offset = r.integer(section, "offset", 0, -99, 99)
        display.minimum = r.integer(section, "minimum", MIN_LEVEL, MIN_LEVEL, MAX_LEVEL)
        display.maximum = r.integer(section, "maximum", MAX_LEVEL, MIN_LEVEL, MAX_LEVEL)
        display.ddc = _read_ddc(r, section, cfg.ddc, DDC_OVERRIDABLE)
        cfg.displays[key] = display

    cfg.validate()
    return cfg


def load(path=None):
    """Read the settings file; a missing file gives the defaults."""
    path = pathlib.Path(path) if path else paths.config_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        cfg = Config()
        cfg.validate()
        return cfg
    except OSError as error:
        raise ConfigError(f"{path}: {error.strerror}") from None
    cfg = parse(text, path)
    cfg.source = path
    return cfg


# Everything the day chart and its handles can change.
CURVE_FIELDS = ("day", "twilight", "night", "mode", "smooth", "margin_minutes",
                "curve_shape", "night_elevation", "twilight_elevation", "day_elevation")


def reset_curve(cfg):
    """Put the curve back to the defaults and leave everything else alone.

    Returns False when the curve already had the default values.
    """
    defaults = Config()
    if all(getattr(cfg, name) == getattr(defaults, name) for name in CURVE_FIELDS):
        return False
    for name in CURVE_FIELDS:
        setattr(cfg, name, getattr(defaults, name))
    return True


# --- writing ---------------------------------------------------------------


def _num(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"
    return str(value)


def render(cfg):
    """The settings as a documented INI file."""
    d = cfg.ddc
    presets = ", ".join(str(p) for p in cfg.tray_pause_presets)
    text = f"""\
# Luxtide settings. The settings window rewrites this file; editing it
# by hand works too, and the daemon picks up every change within a second.

[location]
# Decimal degrees, north and east positive. Leave both empty to use the
# reference city of your time zone: fine for the sun, useless for the
# weather, which then stays off.
latitude = {_num(cfg.latitude)}
longitude = {_num(cfg.longitude)}
# IANA name such as Europe/Warsaw. Empty = follow the system time zone.
timezone = {cfg.timezone}
# yes = update the coordinates from the desktop's location service (GeoClue)
# at start-up and when the network changes, for laptops that travel. Only
# fixes within 3 km are taken, and only moves of 1 km or more are saved.
follow = {_num(cfg.location_follow)}
# How the coordinates were found (manual, geoclue, search, map), the place
# name, the accuracy in metres and when. For information only.
method = {cfg.location_method}
place = {cfg.location_place}
accuracy = {cfg.location_accuracy}
updated = {cfg.location_updated}

[levels]
# Brightness in percent, 1-100. Zero is refused on purpose: on some monitors
# it switches the backlight off and leaves a black screen.
day = {cfg.day}
twilight = {cfg.twilight}
night = {cfg.night}
# One step of Brighter/Dimmer, of the scroll wheel over the tray icon and,
# where the desktop reports them (GNOME 49+, Plasma 6), of the brightness
# keys: this percentage of the current level, at least 1 point.
step = {cfg.step}

[schedule]
# time      = ramps of +/- margin_minutes around sunrise and sunset
# elevation = brightness follows the height of the sun; adapts to the season
#             by itself and also works inside the polar circles
mode = {cfg.mode}
# no = jump between the three levels instead of ramping
smooth = {_num(cfg.smooth)}
margin_minutes = {cfg.margin_minutes}
# Shape of the ramps, 0-1. 0 = linear in brightness units; 1 = geometric,
# which looks even to the eye on displays whose units are linear in light.
curve_shape = {_num(cfg.curve_shape)}
# Sun elevation in degrees, used with mode = elevation.
night_elevation = {_num(cfg.night_elevation)}
twilight_elevation = {_num(cfg.twilight_elevation)}
day_elevation = {_num(cfg.day_elevation)}

[weather]
# Dim on cloudy days. The forecast comes from open-meteo.com, which receives
# your location rounded to 0.01 degree (about 1 km).
enabled = {_num(cfg.weather_enabled)}
# Percent less brightness at 100 % cloud cover. Thinner cloud dims
# proportionally less, and the effect fades out towards night.
strength = {cfg.weather_strength}
refresh_minutes = {cfg.weather_refresh_minutes}
# Ignore a forecast that could not be refreshed for this long.
max_age_hours = {cfg.weather_max_age_hours}

[ddc]
# External monitors over DDC/CI, through ddcutil.
enabled = {_num(d.enabled)}
# Empty = every monitor found. A number limits control to /dev/i2c-N.
bus = {d.bus}
sleep_multiplier = {_num(d.sleep_multiplier)}
# Attempts per transaction when ddcutil reports an error.
retries = {d.retries}
# Identical writes per change. DDC/CI writes are unacknowledged, so on a
# monitor that silently drops frames repetition is the only defence.
repeats = {d.repeats}
settle = {_num(d.settle)}
# ddcutil --syslog level; empty = do not pass the flag (ddcutil < 2.0).
syslog = {d.syslog}
# MCCS version of the monitor (see "ddcutil detect"); empty = autodetect.
mccs = {d.mccs}
skip_ddc_checks = {_num(d.skip_ddc_checks)}
# Some monitors restore their own brightness a few seconds after waking up.
# yes = re-assert the level repeatedly during the first minute after wake.
wake_burst = {_num(d.wake_burst)}
# Re-write the level every N minutes while dimmed; 0 = never.
refresh_minutes = {d.refresh_minutes}
# The level the monitor falls back to on its own. Refreshes are skipped
# whenever the target equals it; wake-up bursts are not, because a monitor
# may also come back at the level it had before.
restore_level = {d.restore_level}

[backlight]
# Built-in laptop panels.
enabled = {_num(cfg.backlight_enabled)}
# Name in /sys/class/backlight; empty = pick automatically.
device = {cfg.backlight_device}
# auto picks the desktop's own interface when there is one, so its brightness
# slider and keys keep working: gnome-shell or mutter (GNOME 49+),
# kde (Plasma 6), then logind; sysfs needs write access.
method = {cfg.backlight_method}
# yes = a change made with the brightness keys holds until the next phase.
follow_manual = {_num(cfg.backlight_follow_manual)}

[tray]
enabled = {_num(cfg.tray_enabled)}
# Show the current percentage: next to the icon where the panel shows text
# (GNOME's AppIndicator extension, Ayatana indicators), elsewhere drawn as
# the icon itself (Plasma, XFCE, Cinnamon, LXQt, waybar…).
show_level = {_num(cfg.tray_show_level)}
# Pause durations offered in the menu, in minutes.
pause_presets = {presets}
"""
    for key in sorted(cfg.displays):
        display = cfg.displays[key]
        if display.is_default():
            continue
        lines = [f"\n[display:{key}]"]
        if not display.enabled:
            lines.append("enabled = no")
        if display.offset:
            lines.append(f"offset = {display.offset}")
        if display.minimum != MIN_LEVEL:
            lines.append(f"minimum = {display.minimum}")
        if display.maximum != MAX_LEVEL:
            lines.append(f"maximum = {display.maximum}")
        for name in DDC_OVERRIDABLE:
            if name in display.ddc:
                lines.append(f"{name} = {_num(display.ddc[name])}")
        text += "\n".join(lines) + "\n"
    return "\n".join(line.rstrip() for line in text.split("\n"))


def save(cfg, path=None):
    path = pathlib.Path(path) if path else (cfg.source or paths.config_file())
    paths.atomic_write(path, render(cfg))
    cfg.source = path
    return path
