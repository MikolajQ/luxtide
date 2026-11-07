"""Cloud-cover forecast from Open-Meteo, cached on disk.

Open-Meteo is free for non-commercial use, needs no key and serves the
best local model for each place (around Poland that is DWD ICON-D2 at 2 km,
refreshed every three hours). We request only hourly total cloud cover, in
UTC unix time, and interpolate between the hours so the correction drifts
instead of jumping.

Coordinates are rounded to 0.01 degree (about 1 km) before they leave the
machine: the forecast grid is coarser than that anyway.
"""

import bisect
import dataclasses
import json
import time
import urllib.parse
import urllib.request

from . import __version__, paths

API_URL = "https://api.open-meteo.com/v1/forecast"
PROVIDER = "Open-Meteo"


class WeatherError(RuntimeError):
    pass


def round_coordinate(value):
    return round(value, 2)


@dataclasses.dataclass
class Forecast:
    fetched_at: float
    latitude: float
    longitude: float
    times: list                  # unix seconds, ascending, hourly
    cloud: list                  # percent, same length

    def cloud_at(self, timestamp):
        """Cloud cover in percent at `timestamp`, or None outside the forecast."""
        if not self.times or timestamp < self.times[0] or timestamp > self.times[-1]:
            return None
        index = bisect.bisect_right(self.times, timestamp) - 1
        if index >= len(self.times) - 1:
            return float(self.cloud[-1])
        t0, t1 = self.times[index], self.times[index + 1]
        c0, c1 = self.cloud[index], self.cloud[index + 1]
        return c0 + (c1 - c0) * (timestamp - t0) / (t1 - t0)

    def covers(self, latitude, longitude):
        return (round_coordinate(latitude) == self.latitude
                and round_coordinate(longitude) == self.longitude)

    def usable(self, now, latitude, longitude, max_age_hours):
        return (self.covers(latitude, longitude)
                and now - self.fetched_at <= max_age_hours * 3600
                and self.cloud_at(now) is not None)

    def to_json(self):
        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        forecast = cls(float(data["fetched_at"]), float(data["latitude"]),
                       float(data["longitude"]), [int(t) for t in data["times"]],
                       [float(c) for c in data["cloud"]])
        if len(forecast.times) != len(forecast.cloud):
            raise ValueError("times and cloud differ in length")
        return forecast


def build_url(latitude, longitude):
    query = urllib.parse.urlencode({
        "latitude": f"{round_coordinate(latitude):.2f}",
        "longitude": f"{round_coordinate(longitude):.2f}",
        "hourly": "cloud_cover",
        "forecast_days": 2,
        "timezone": "UTC",
        "timeformat": "unixtime",
    })
    return f"{API_URL}?{query}"


def parse_response(data, latitude, longitude, fetched_at):
    try:
        hourly = data["hourly"]
        pairs = [(int(t), float(c)) for t, c in zip(hourly["time"], hourly["cloud_cover"])
                 if c is not None]
    except (KeyError, TypeError, ValueError) as error:
        raise WeatherError(f"unexpected response: {error}") from None
    if len(pairs) < 2:
        raise WeatherError("forecast has no cloud cover data")
    pairs.sort()
    return Forecast(fetched_at, round_coordinate(latitude), round_coordinate(longitude),
                    [t for t, _ in pairs], [max(0.0, min(100.0, c)) for _, c in pairs])


def fetch(latitude, longitude, timeout=20, opener=urllib.request.urlopen):
    """Download a fresh forecast. Blocking; call it off the main loop."""
    request = urllib.request.Request(
        build_url(latitude, longitude),
        headers={"User-Agent": f"ddc-sun-dimmer/{__version__}"})
    try:
        with opener(request, timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError) as error:
        raise WeatherError(str(error)) from None
    if isinstance(data, dict) and data.get("error"):
        raise WeatherError(str(data.get("reason", "request refused")))
    return parse_response(data, latitude, longitude, time.time())


def cache_file():
    return paths.cache_dir() / "forecast.json"


def load_cache(path=None):
    try:
        return Forecast.from_json((path or cache_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cache(forecast, path=None):
    paths.atomic_write(path or cache_file(), forecast.to_json())


def correction(base, cloud, strength, weight):
    """Multiplier for the scheduled level: 1 = no change.

    `strength` is the percent reduction at full cloud cover; `weight` fades
    the effect between night (0) and full day (1).
    """
    if cloud is None or strength <= 0:
        return 1.0
    return 1.0 - (strength / 100.0) * (max(0.0, min(100.0, cloud)) / 100.0) * weight
