"""Who decides the level right now: the sun, the weather, or the user.

Priority, highest first:

    preview   a few seconds of "try this level" from the settings window
    pause     the automation is frozen; manual levels are still accepted
    override  a manual level that holds until the next phase or a set time
    schedule  the sun, corrected for cloud cover

Pure logic with explicit time, so it is easy to test.
"""

import dataclasses
import json

from . import clamp_level, weather

FOREVER = 0           # "until" values understood by set_level() and pause()
NEXT_PHASE = -1
PREVIEW_SECONDS = 20  # a preview lapses unless the settings window renews it
NO_PHASE_FALLBACK = 12 * 3600


@dataclasses.dataclass
class Hold:
    level: int | None
    until: float | None                    # unix time; None = until cancelled

    def expired(self, now):
        return self.until is not None and now >= self.until


@dataclasses.dataclass
class State:
    override: Hold | None = None
    pause: Hold | None = None
    preview: Hold | None = None

    def expire(self, now):
        """Drop lapsed holds. True when anything changed."""
        changed = False
        for name in ("override", "pause", "preview"):
            hold = getattr(self, name)
            if hold is not None and hold.expired(now):
                setattr(self, name, None)
                changed = True
        return changed

    def to_json(self):
        data = {name: dataclasses.asdict(getattr(self, name))
                for name in ("override", "pause") if getattr(self, name)}
        return json.dumps(data)

    @classmethod
    def from_json(cls, text, now):
        state = cls()
        try:
            data = json.loads(text)
            for name in ("override", "pause"):
                if name in data:
                    item = data[name]
                    level = item.get("level")
                    until = item.get("until")
                    setattr(state, name, Hold(None if level is None else clamp_level(level),
                                              None if until is None else float(until)))
        except (ValueError, TypeError, AttributeError):
            return cls()
        state.expire(now)
        return state


@dataclasses.dataclass
class Decision:
    level: int               # what the displays should show (before per-display offsets)
    scheduled: int           # what the sun and weather alone ask for
    base: float              # the sun alone, unrounded
    factor: float            # weather multiplier, 1 = none
    cloud: float | None
    mode: str                # "auto", "override", "paused" or "preview"
    until: float | None      # when the current hold ends
    phase: str


def scheduled(schedule, timestamp, cloud, cfg):
    """(level, base, factor) from the sun and the weather."""
    base = schedule.base(timestamp)
    factor = 1.0
    if cfg.weather_enabled and cloud is not None:
        factor = weather.correction(base, cloud, cfg.weather_strength,
                                    schedule.daylight_weight(base))
    # Clouds may dim the day, but never below what the night asks for.
    level = min(base, max(cfg.night, base * factor))
    return clamp_level(level), base, factor


def decide(schedule, state, timestamp, cloud, cfg):
    level, base, factor = scheduled(schedule, timestamp, cloud, cfg)
    common = dict(scheduled=level, base=base, factor=factor, cloud=cloud,
                  phase=schedule.phase(timestamp))
    if state.preview is not None:
        return Decision(level=state.preview.level, mode="preview",
                        until=state.preview.until, **common)
    if state.pause is not None:
        held = state.pause.level if state.pause.level is not None else level
        return Decision(level=held, mode="paused", until=state.pause.until, **common)
    if state.override is not None:
        return Decision(level=state.override.level, mode="override",
                        until=state.override.until, **common)
    return Decision(level=level, mode="auto", until=None, **common)


def resolve_until(value, now, schedule):
    """Turn FOREVER / NEXT_PHASE / a unix time into an absolute time or None."""
    if value == FOREVER:
        return None
    if value == NEXT_PHASE or value is None:
        change = schedule.next_phase_change(now)
        return change if change is not None else now + NO_PHASE_FALLBACK
    if value <= now:
        raise ValueError("that moment has already passed")
    return float(value)


def set_level(state, level, until):
    """Manual level. Inside a pause it changes the paused level instead."""
    level = clamp_level(level)
    if state.pause is not None:
        state.pause.level = level
    else:
        state.override = Hold(level, until)


def pause(state, current_level, until):
    """Freeze at the current level. Pausing again only moves the end time."""
    if state.pause is not None:
        state.pause.until = until
    else:
        state.pause = Hold(clamp_level(current_level), until)
    state.override = None


def resume(state):
    state.pause = None
    state.override = None
    state.preview = None


def preview(state, level, now):
    state.preview = Hold(clamp_level(level), now + PREVIEW_SECONDS)


def step_level(level, direction, percent, presses=1):
    """One step brighter (+1) or dimmer (-1): `percent` of the current level,
    at least one point. The steps are even to the eye and get fine near the
    bottom, where a fixed 5 points would go from 6 % straight to 1 %. Up and
    down retrace each other: 50 -> 55 -> 50 with 10 %."""
    factor = 1.0 + percent / 100.0
    for _ in range(max(1, presses)):
        if direction > 0:
            level = max(level + 1, round(level * factor))
        elif direction < 0:
            level = min(level - 1, round(level / factor))
        level = clamp_level(level)
    return level


def next_change(schedule, state, now, cloud_at, cfg, horizon=26 * 3600):
    """(unix time, level) of the next change of the decided level, or (None, None)."""
    current = decide(schedule, state, now, cloud_at(now), cfg)
    if current.mode != "auto":
        if current.until is None:
            return None, None
        after = State(override=state.override if current.mode == "preview" else None)
        if current.mode == "preview" and state.pause is not None:
            after = State(pause=state.pause)
        later = decide(schedule, after, current.until, cloud_at(current.until), cfg)
        return current.until, later.level
    moment = now - now % 60
    while moment - now < horizon:
        moment += 60
        level, _, _ = scheduled(schedule, moment, cloud_at(moment), cfg)
        if level != current.level:
            return moment, level
    return None, None
