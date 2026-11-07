"""The daily brightness curve: what level the sun alone asks for.

Two ways to place the ramps:

* time      - ramps of +/- margin around sunrise and sunset, passing through
              the twilight level exactly at sunrise and sunset;
* elevation - level as a function of the sun's height, which stretches the
              ramps in summer and squeezes them in winter by itself.

Ramps are shaped by `curve_shape`. Perceived brightness grows much more slowly
than light output, so a ramp that is linear in brightness units barely moves
for most of its length and then collapses at the end. Interpolating in a
compressed space spreads the change evenly for the eye:

    shape 0      linear
    shape ~0.67  cube root, close to CIE L* lightness
    shape 1      logarithmic, i.e. geometric interpolation
"""

import bisect
import datetime as dt
import math

from . import solar

PHASES = ("night", "dawn", "day", "dusk")
MINUTE = 60.0
DAY = 86400.0


def shape_functions(shape):
    """(forward, inverse) maps of the interpolation space for a curve shape."""
    exponent = 1.0 - max(0.0, min(1.0, shape))
    if exponent < 1e-3:
        return math.log, math.exp
    return (lambda x: x ** exponent), (lambda y: max(y, 0.0) ** (1.0 / exponent))


def interpolate(start, end, progress, shape):
    """Level `progress` (0-1) of the way from `start` to `end`. Levels are >= 1."""
    if progress <= 0 or start == end:
        return start
    if progress >= 1:
        return end
    forward, inverse = shape_functions(shape)
    value = inverse(forward(start) + (forward(end) - forward(start)) * progress)
    # exp(log(x)) is not always exactly x; never overshoot the endpoints.
    return max(min(start, end), min(max(start, end), value))


class Schedule:
    """Brightness the sun asks for at any moment, for one configuration."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.latitude, self.longitude, self.location_source = cfg.location()
        self.tz = cfg.tz()
        self._sun = {}
        self._anchors = {}

    # --- sun ---------------------------------------------------------------

    def sun(self, date):
        if date not in self._sun:
            self._sun[date] = solar.sun_day(date, self.latitude, self.longitude, self.tz)
        return self._sun[date]

    def elevation(self, timestamp):
        return solar.elevation(timestamp, self.latitude, self.longitude)

    def local_date(self, timestamp):
        return dt.datetime.fromtimestamp(timestamp, self.tz).date()

    def midnight(self, date):
        """Unix time of local midnight that starts `date`."""
        return dt.datetime.combine(date, dt.time(0), self.tz).timestamp()

    # --- time mode ---------------------------------------------------------

    def anchors(self, date):
        """(time, level, phase) points for the dates around `date`, sorted.

        `phase` is the phase that begins at that point. Margins shrink where
        a day or a night is shorter than two margins, so that the ramps of
        consecutive events never cross.
        """
        if date in self._anchors:
            return self._anchors[date]
        c = self.cfg
        margin = c.margin_minutes * MINUTE
        suns = {offset: self.sun(date + dt.timedelta(days=offset)) for offset in range(-2, 3)}
        points = []
        for offset in (-1, 0, 1):
            day = date + dt.timedelta(days=offset)
            s = suns[offset]
            if s.polar:
                level = c.day if s.polar == "day" else c.night
                start = self.midnight(day)
                points += [(start, level, s.polar), (start + DAY - 1, level, s.polar)]
                continue
            rise, set_ = s.rise.timestamp(), s.set.timestamp()
            previous, following = suns[offset - 1], suns[offset + 1]
            half_day = (set_ - rise) / 2
            rise_margin = min(margin, half_day)
            set_margin = min(margin, half_day)
            if not previous.polar:
                rise_margin = min(rise_margin, (rise - previous.set.timestamp()) / 2)
            if not following.polar:
                set_margin = min(set_margin, (following.rise.timestamp() - set_) / 2)
            if c.smooth:
                points += [
                    (rise - rise_margin, c.night, "dawn"),
                    (rise, c.twilight, "dawn"),
                    (rise + rise_margin, c.day, "day"),
                    (set_ - set_margin, c.day, "dusk"),
                    (set_, c.twilight, "dusk"),
                    (set_ + set_margin, c.night, "night"),
                ]
            else:
                points += [
                    (rise - rise_margin, c.twilight, "dawn"),
                    (rise + rise_margin, c.day, "day"),
                    (set_ - set_margin, c.twilight, "dusk"),
                    (set_ + set_margin, c.night, "night"),
                ]
        points.sort(key=lambda point: point[0])
        if len(self._anchors) > 16:
            self._anchors.clear()
        self._anchors[date] = points
        return points

    def _time_base(self, timestamp):
        points = self.anchors(self.local_date(timestamp))
        times = [p[0] for p in points]
        index = bisect.bisect_right(times, timestamp)
        if index == 0:
            return points[0][1]
        if index == len(points):
            return points[-1][1]
        start, start_level, _ = points[index - 1]
        end, end_level, _ = points[index]
        if not self.cfg.smooth:
            return start_level
        span = end - start
        if span <= 0:
            return end_level
        return interpolate(start_level, end_level, (timestamp - start) / span,
                           self.cfg.curve_shape)

    def _time_phase(self, timestamp):
        points = self.anchors(self.local_date(timestamp))
        index = bisect.bisect_right([p[0] for p in points], timestamp)
        return points[index - 1][2] if index else "night"

    # --- elevation mode ----------------------------------------------------

    def _elevation_base(self, timestamp):
        c = self.cfg
        e = self.elevation(timestamp)
        if not c.smooth:
            if e < c.night_elevation:
                return c.night
            return c.twilight if e < c.day_elevation else c.day
        if e <= c.night_elevation:
            return c.night
        if e < c.twilight_elevation:
            return interpolate(c.night, c.twilight, (e - c.night_elevation)
                               / (c.twilight_elevation - c.night_elevation), c.curve_shape)
        if e < c.day_elevation:
            return interpolate(c.twilight, c.day, (e - c.twilight_elevation)
                               / (c.day_elevation - c.twilight_elevation), c.curve_shape)
        return c.day

    def _elevation_phase(self, timestamp):
        c = self.cfg
        e = self.elevation(timestamp)
        if e < c.night_elevation or (c.smooth and e == c.night_elevation):
            return "night"
        if e >= c.day_elevation:
            return "day"
        return "dawn" if self.elevation(timestamp + 300) > e else "dusk"

    # --- public ------------------------------------------------------------

    def base(self, timestamp):
        """Unrounded level the sun asks for, before weather."""
        if self.cfg.mode == "elevation":
            return self._elevation_base(timestamp)
        return self._time_base(timestamp)

    def phase(self, timestamp):
        if self.cfg.mode == "elevation":
            return self._elevation_phase(timestamp)
        return self._time_phase(timestamp)

    def next_phase_change(self, timestamp, horizon=26 * 3600):
        """When the phase changes next, as unix time, or None within `horizon`."""
        current = self.phase(timestamp)
        if self.cfg.mode == "time":
            for date in (self.local_date(timestamp), self.local_date(timestamp + DAY)):
                for when, _, phase in self.anchors(date):
                    if when > timestamp and phase != current and when - timestamp <= horizon:
                        return when
            return None
        step = MINUTE
        moment = timestamp
        while moment - timestamp < horizon:
            moment += step
            if self.phase(moment) != current:
                # Refine to the second, so expiries line up with the ramp.
                low, high = moment - step, moment
                while high - low > 1:
                    middle = (low + high) / 2
                    if self.phase(middle) == current:
                        low = middle
                    else:
                        high = middle
                return high
        return None

    def daylight_weight(self, level):
        """0 at the night level, 1 at the day level, measured in curve space.

        Used to fade the weather correction out towards night, when cloud
        cover makes no difference to the room.
        """
        night, day = self.cfg.night, self.cfg.day
        if day <= night:
            return 1.0
        forward, _ = shape_functions(self.cfg.curve_shape)
        weight = (forward(max(level, 1e-6)) - forward(night)) / (forward(day) - forward(night))
        return max(0.0, min(1.0, weight))

    def profile(self, date, step_minutes=5):
        """[(unix time, base level)] across one local date, for charts."""
        start = self.midnight(date)
        end = self.midnight(date + dt.timedelta(days=1))
        points = []
        moment = start
        while moment <= end:
            points.append((moment, self.base(moment)))
            moment += step_minutes * MINUTE
        return points
