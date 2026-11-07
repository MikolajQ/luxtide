"""The day chart: model building and Cairo drawing, free of GTK.

Kept apart from the widget so it can be rendered straight to a PNG for
checks and documentation.
"""

import dataclasses
import datetime as dt
import math

from .. import clamp_level, policy
from ..curve import Schedule

STEP_MINUTES = 5


@dataclasses.dataclass
class Handle:
    name: str              # "day", "night", "twilight", "margin"
    timestamp: float
    level: float
    axis: str              # "y" = drag up and down, "x" = drag sideways


@dataclasses.dataclass
class ChartModel:
    start: float
    end: float
    tz: object
    base: list
    adjusted: list | None
    cloud: list
    elevation: list
    sunrise: float | None
    sunset: float | None
    now: float | None
    handles: list
    perceived: bool = True
    hover: tuple | None = None
    active: str | None = None


def lightness(level):
    """CIE L* of a relative luminance in percent, scaled back to 0-100."""
    y = max(0.0, level) / 100.0
    if y > 0.008856:
        return 116.0 * y ** (1 / 3) - 16.0
    return 903.3 * y


def from_lightness(value):
    value = max(0.0, min(100.0, value))
    if value > 8.0:
        return 100.0 * ((value + 16.0) / 116.0) ** 3
    return 100.0 * value / 903.3


def _crossing(points, threshold, rising):
    """First time a sampled series crosses `threshold` in one direction."""
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if rising and v0 < threshold <= v1 or not rising and v0 >= threshold > v1:
            return t0 + (t1 - t0) * ((threshold - v0) / (v1 - v0) if v1 != v0 else 0)
    return None


def build_model(cfg, date, cloud_points=(), now=None, perceived=True):
    schedule = Schedule(cfg)
    start = schedule.midnight(date)
    end = schedule.midnight(date + dt.timedelta(days=1))
    base = schedule.profile(date, STEP_MINUTES)

    def cloud_at(timestamp):
        for (t0, c0), (t1, c1) in zip(cloud_points, cloud_points[1:]):
            if t0 <= timestamp <= t1:
                return c0 + (c1 - c0) * (timestamp - t0) / (t1 - t0)
        return None

    adjusted = None
    clouds = [(t, c) for t, c in cloud_points if start - 3600 <= t <= end + 3600]
    if cfg.weather_enabled and clouds:
        adjusted = []
        for timestamp, _ in base:
            cloud = cloud_at(timestamp)
            level, _, _ = policy.scheduled(schedule, timestamp, cloud, cfg)
            adjusted.append((timestamp, level))

    elevation = [(t, schedule.elevation(t)) for t in range(int(start), int(end) + 1, 600)]
    sun = schedule.sun(date)
    rise = sun.rise.timestamp() if sun.rise else None
    set_ = sun.set.timestamp() if sun.set else None

    handles = []
    noon = (rise + set_) / 2 if rise and set_ else start + 43200
    if sun.polar != "night":
        handles.append(Handle("day", noon, cfg.day, "y"))
    if sun.polar != "day":
        night_at = start + (rise - start) / 2 if rise else start + 3 * 3600
        handles.append(Handle("night", night_at, cfg.night, "y"))
    if rise and set_:
        if cfg.mode == "time":
            twilight_times = (rise, set_)
        else:
            twilight_times = (_crossing(elevation, cfg.twilight_elevation, True),
                              _crossing(elevation, cfg.twilight_elevation, False))
        for timestamp in twilight_times:
            if timestamp is not None:
                handles.append(Handle("twilight", timestamp, cfg.twilight, "y"))
        if cfg.mode == "time":
            margin = cfg.margin_minutes * 60
            handles.append(Handle("margin", min(rise + margin, noon), cfg.day, "x"))
            handles.append(Handle("margin", max(set_ - margin, noon), cfg.day, "x"))

    return ChartModel(start, end, schedule.tz, base, adjusted, clouds, elevation, rise, set_,
                      now if now is not None and start <= now <= end else None, handles,
                      perceived)


@dataclasses.dataclass
class Geometry:
    width: float
    height: float
    model: ChartModel
    left: float = 44
    right: float = 14
    top: float = 14
    bottom: float = 30

    @property
    def plot_width(self):
        return max(1.0, self.width - self.left - self.right)

    @property
    def plot_height(self):
        return max(1.0, self.height - self.top - self.bottom)

    def x(self, timestamp):
        m = self.model
        return self.left + (timestamp - m.start) / (m.end - m.start) * self.plot_width

    def timestamp(self, x):
        m = self.model
        fraction = (x - self.left) / self.plot_width
        return m.start + max(0.0, min(1.0, fraction)) * (m.end - m.start)

    def y(self, level):
        value = lightness(level) if self.model.perceived else level
        return self.top + (1 - value / 100.0) * self.plot_height

    def level(self, y):
        value = (1 - (y - self.top) / self.plot_height) * 100.0
        value = from_lightness(value) if self.model.perceived else value
        return clamp_level(value)


def _rgba(cr, color, alpha=1.0):
    cr.set_source_rgba(color[0], color[1], color[2], color[3] * alpha)


def draw(cr, width, height, model, fg=(0.2, 0.2, 0.2, 1.0), accent=(0.21, 0.52, 0.89, 1.0)):
    g = Geometry(width, height, model)
    top, bottom = g.top, g.top + g.plot_height
    left, right = g.left, g.left + g.plot_width

    # Night and twilight bands, from the sun's elevation, merged into runs so
    # that translucent rectangles do not overlap into stripes.
    def band(elevation):
        return 0.10 if elevation < -6 else 0.05 if elevation < 0 else 0.0

    runs = []
    for (t0, e0), (t1, _) in zip(model.elevation, model.elevation[1:]):
        alpha = band(e0)
        if runs and runs[-1][2] == alpha and runs[-1][1] == t0:
            runs[-1][1] = t1
        else:
            runs.append([t0, t1, alpha])
    for t0, t1, alpha in runs:
        if alpha:
            _rgba(cr, fg, alpha)
            cr.rectangle(g.x(t0), top, g.x(t1) - g.x(t0), bottom - top)
            cr.fill()

    # Cloud cover as faint bars from the bottom, a quarter of the height at most.
    for (t0, c0), (t1, _) in zip(model.cloud, model.cloud[1:]):
        x0, x1 = max(left, g.x(t0)), min(right, g.x(t1))
        if x1 <= x0:
            continue
        h = (bottom - top) * 0.25 * c0 / 100.0
        cr.set_source_rgba(0.55, 0.6, 0.68, 0.30)
        cr.rectangle(x0, bottom - h, x1 - x0 - 1, h)
        cr.fill()

    # Grid and labels.
    cr.set_line_width(1)
    cr.select_font_face("sans-serif")
    cr.set_font_size(10)
    for level in (1, 10, 25, 50, 75, 100):
        y = round(g.y(level)) + 0.5
        _rgba(cr, fg, 0.12)
        cr.move_to(left, y)
        cr.line_to(right, y)
        cr.stroke()
        _rgba(cr, fg, 0.55)
        text = f"{level}%"
        extents = cr.text_extents(text)
        cr.move_to(left - 6 - extents.width, y + extents.height / 2 - 1)
        cr.show_text(text)
    for hour in range(0, 25, 3):
        timestamp = dt.datetime.combine(dt.datetime.fromtimestamp(model.start, model.tz).date(),
                                        dt.time(0), model.tz).timestamp() + hour * 3600
        x = round(g.x(min(timestamp, model.end))) + 0.5
        _rgba(cr, fg, 0.12)
        cr.move_to(x, top)
        cr.line_to(x, bottom)
        cr.stroke()
        _rgba(cr, fg, 0.55)
        text = f"{hour:02d}"
        extents = cr.text_extents(text)
        cr.move_to(x - extents.width / 2, bottom + 15)
        cr.show_text(text)

    # Sunrise and sunset, labelled on the night side of the line so the
    # ramp handles on the day side never cover the time.
    for timestamp, side in ((model.sunrise, -1), (model.sunset, 1)):
        if timestamp is None:
            continue
        x = g.x(timestamp)
        _rgba(cr, fg, 0.35)
        cr.set_dash([3, 3])
        cr.move_to(x, top)
        cr.line_to(x, bottom)
        cr.stroke()
        cr.set_dash([])
        text = dt.datetime.fromtimestamp(timestamp, model.tz).strftime("%H:%M")
        extents = cr.text_extents(text)
        _rgba(cr, fg, 0.75)
        cr.move_to(x + 5 if side > 0 else x - 5 - extents.width, bottom - 6)
        cr.show_text(text)

    def path(points):
        for index, (timestamp, level) in enumerate(points):
            (cr.line_to if index else cr.move_to)(g.x(timestamp), g.y(level))

    # The sun alone: dashed when the weather changes it, solid otherwise.
    cr.set_line_join(1)
    if model.adjusted:
        _rgba(cr, fg, 0.45)
        cr.set_dash([5, 4])
        cr.set_line_width(1.5)
        path(model.base)
        cr.stroke()
        cr.set_dash([])
        curve = model.adjusted
    else:
        curve = model.base
    # Area under the curve.
    path(curve)
    cr.line_to(g.x(curve[-1][0]), bottom)
    cr.line_to(g.x(curve[0][0]), bottom)
    cr.close_path()
    _rgba(cr, accent, 0.14)
    cr.fill()
    _rgba(cr, accent)
    cr.set_line_width(2.5)
    path(curve)
    cr.stroke()

    # Now.
    if model.now is not None:
        x = g.x(model.now)
        cr.set_source_rgba(0.88, 0.35, 0.15, 0.9)
        cr.set_line_width(1.5)
        cr.move_to(x, top)
        cr.line_to(x, bottom)
        cr.stroke()

    # Handles.
    for handle in model.handles:
        x, y = g.x(handle.timestamp), g.y(handle.level)
        radius = 7 if handle.name == model.active else 5.5
        if handle.axis == "x":
            cr.rectangle(x - radius, y - radius, radius * 2, radius * 2)
        else:
            cr.arc(x, y, radius, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 1)
        cr.fill_preserve()
        _rgba(cr, accent)
        cr.set_line_width(2)
        cr.stroke()

    # Hover read-out.
    if model.hover is not None:
        timestamp, level = model.hover
        x = g.x(timestamp)
        _rgba(cr, fg, 0.4)
        cr.set_line_width(1)
        cr.move_to(x, top)
        cr.line_to(x, bottom)
        cr.stroke()
        text = (f"{dt.datetime.fromtimestamp(timestamp, model.tz):%H:%M}  "
                f"{clamp_level(level)} %")
        cr.set_font_size(11)
        extents = cr.text_extents(text)
        bx = min(max(x + 8, left), right - extents.width - 12)
        by = top + 18
        cr.set_source_rgba(0.1, 0.1, 0.1, 0.85)
        cr.rectangle(bx, by, extents.width + 10, extents.height + 8)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.move_to(bx + 5, by + extents.height + 3)
        cr.show_text(text)


def value_at(points, timestamp):
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= timestamp <= t1:
            return v0 + (v1 - v0) * (timestamp - t0) / (t1 - t0) if t1 > t0 else v1
    return points[-1][1] if points else 0


def hit(model, width, height, x, y, radius=10):
    """The handle under the pointer, or None."""
    g = Geometry(width, height, model)
    best = None
    for handle in model.handles:
        distance = math.hypot(g.x(handle.timestamp) - x, g.y(handle.level) - y)
        if distance <= radius and (best is None or distance < best[0]):
            best = (distance, handle)
    return best[1] if best else None
