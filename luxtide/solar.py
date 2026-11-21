"""Sun position from the system clock alone: no network, no dependencies.

Sunrise and sunset come from the "sunrise equation" (a simplified NOAA
algorithm); elevation comes from the low-precision solar coordinates of the
Astronomical Almanac. Both are good to about a minute or a hundredth of a
degree, far finer than any brightness curve needs.
"""

import dataclasses
import datetime as dt
import math
import os
import pathlib
import re
import zoneinfo

J2000 = 2451545.0
UNIX_EPOCH_JD = 2440587.5

# The sun counts as risen when its upper limb clears the horizon: half a
# degree for the solar radius plus about a third of a degree of refraction.
HORIZON = -0.833

ZONEINFO_DIR = pathlib.Path("/usr/share/zoneinfo")


@dataclasses.dataclass(frozen=True)
class SunDay:
    """Sunrise and sunset of one local date.

    Inside the polar circles the sun may stay up or down all day. Then both
    times are None and `polar` says which of the two it is.
    """

    rise: dt.datetime | None
    set: dt.datetime | None
    polar: str | None = None          # "day", "night" or None


def sun_day(date, latitude, longitude, tz):
    """Sunrise and sunset for a local date as aware datetimes in `tz`.

    The sunrise equation gives a first estimate that can be up to three
    minutes late; each event is then refined against the elevation model,
    which agrees with NOAA's full algorithm to a few seconds.
    """
    latitude = max(-89.99, min(89.99, latitude))
    n = (date - dt.date(2000, 1, 1)).days + 0.0009 - longitude / 360.0
    m = math.radians((357.5291 + 0.98560028 * n) % 360)
    c = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    transit = J2000 + n + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))

    lat = math.radians(latitude)
    cos_w = (math.sin(math.radians(HORIZON)) - math.sin(lat) * math.sin(decl)) / (
        math.cos(lat) * math.cos(decl))
    if cos_w > 1:
        return SunDay(None, None, "night")
    if cos_w < -1:
        return SunDay(None, None, "day")

    half_day = math.degrees(math.acos(cos_w)) / 360.0
    rise = _refine(_julian_to_unix(transit - half_day), latitude, longitude, rising=True)
    set_ = _refine(_julian_to_unix(transit + half_day), latitude, longitude, rising=False)
    return SunDay(dt.datetime.fromtimestamp(rise, tz), dt.datetime.fromtimestamp(set_, tz))


def _refine(estimate, latitude, longitude, rising, window=1800):
    """The moment within +/- window of `estimate` when the sun crosses HORIZON."""
    low, high = estimate - window, estimate + window
    below_low = elevation(low, latitude, longitude) < HORIZON
    below_high = elevation(high, latitude, longitude) < HORIZON
    if below_low == below_high or below_low != rising:
        return estimate          # grazing the horizon near the polar circle: keep it
    for _ in range(24):          # 3600 s / 2**24: far below a second
        middle = (low + high) / 2
        if (elevation(middle, latitude, longitude) < HORIZON) == rising:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _julian_to_unix(julian):
    return (julian - UNIX_EPOCH_JD) * 86400.0


def elevation(timestamp, latitude, longitude):
    """Geometric elevation of the sun's centre in degrees, without refraction."""
    n = timestamp / 86400.0 + UNIX_EPOCH_JD - J2000
    mean_longitude = (280.460 + 0.9856474 * n) % 360
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic = math.radians(mean_longitude + 1.915 * math.sin(anomaly)
                            + 0.020 * math.sin(2 * anomaly))
    obliquity = math.radians(23.439 - 0.0000004 * n)

    right_ascension = math.atan2(math.cos(obliquity) * math.sin(ecliptic),
                                 math.cos(ecliptic))
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic))
    sidereal_hours = (18.697374558 + 24.06570982441908 * n) % 24
    hour_angle = math.radians(sidereal_hours * 15 + longitude) - right_ascension

    lat = math.radians(latitude)
    sine = (math.sin(lat) * math.sin(declination)
            + math.cos(lat) * math.cos(declination) * math.cos(hour_angle))
    return math.degrees(math.asin(max(-1.0, min(1.0, sine))))


# --- time zones -------------------------------------------------------------


def system_timezone_name():
    """IANA name of the system time zone, e.g. 'Europe/Warsaw', or None."""
    env = os.environ.get("TZ", "").lstrip(":")
    if env and "/" in env and not env.startswith("/"):
        return env
    try:
        parts = pathlib.Path("/etc/localtime").resolve().parts
    except OSError:
        return None
    if "zoneinfo" in parts:
        index = len(parts) - 1 - parts[::-1].index("zoneinfo")
        name = "/".join(parts[index + 1:])
        # Some distributions keep "posix/" and "right/" variants side by side.
        for prefix in ("posix/", "right/"):
            if name.startswith(prefix):
                name = name[len(prefix):]
        return name or None
    return None


def load_timezone(name=None):
    """A ZoneInfo for `name`, or for the system zone when `name` is empty."""
    if name:
        return zoneinfo.ZoneInfo(name)
    system = system_timezone_name()
    if system:
        try:
            return zoneinfo.ZoneInfo(system)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            pass
    try:
        with open("/etc/localtime", "rb") as handle:
            return zoneinfo.ZoneInfo.from_file(handle, key="localtime")
    except OSError:
        return dt.timezone.utc


def zone_coordinates(zone, directory=ZONEINFO_DIR):
    """Coordinates of the principal city of a time zone, from zone1970.tab.

    Good enough for a zero-configuration start: within one zone sunrise
    rarely moves by more than half an hour, and the brightness ramp is an
    hour wide anyway.
    """
    if not zone:
        return None
    for table in ("zone1970.tab", "zone.tab"):
        path = pathlib.Path(directory) / table
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 3 and fields[2] == zone:
                return parse_iso6709(fields[1])
    return None


def parse_iso6709(text):
    """'+5215+02100' or '+404251-0740023' -> (latitude, longitude)."""
    match = re.fullmatch(r"([+-]\d{4,6})([+-]\d{5,7})", text.strip())
    if not match:
        raise ValueError(f"not an ISO 6709 coordinate: {text!r}")
    return _sexagesimal(match.group(1), 2), _sexagesimal(match.group(2), 3)


def _sexagesimal(text, degree_digits):
    sign = -1 if text[0] == "-" else 1
    digits = text[1:]
    degrees = int(digits[:degree_digits])
    minutes = int(digits[degree_digits:degree_digits + 2])
    seconds = int(digits[degree_digits + 2:] or 0)
    return sign * (degrees + minutes / 60 + seconds / 3600)
