"""Command line: `ddc-sun-dimmer <command>` and the short `dim` front end."""

import datetime as dt
import json
import logging
import os
import re
import sys
import time

from . import __version__, clamp_level, config as config_module, displays, policy, weather

DIM_HELP = """\
dim - set display brightness by hand, next to the automatic schedule

  dim 40           40 % until the next phase (sunrise, sunset, ramp end)
  dim +10, dim -10 brighter or dimmer by 10 points
  dim -c 60        DDC contrast to 60 (never touched by the schedule)
  dim status       what is set and what comes next
  dim pause [WHEN] freeze the schedule; WHEN = 30m, 1h30m, 21:00, phase
                   (default: until resumed)
  dim resume       back to the schedule right away

Without a running daemon, levels are written directly. DIM_REPEATS=5 dim 40
repeats each DDC write five times.
"""


# --- parsing helpers -------------------------------------------------------


def parse_when(text, now=None):
    """'30m', '1h30m', '90', '21:00', 'phase', 'forever' -> policy until value."""
    now = now or time.time()
    t = text.strip().lower()
    if t in ("", "forever", "resume", "inf", "indefinitely", "until-resumed"):
        return policy.FOREVER
    if t in ("phase", "next", "next-phase"):
        return policy.NEXT_PHASE
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"no such time: {text}")
        local = dt.datetime.fromtimestamp(now).astimezone()
        target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += dt.timedelta(days=1)
        return int(target.timestamp())
    if t.isdigit():
        return int(now + int(t) * 60)
    match = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*(?:m|min)?)?", t)
    if match and (match.group(1) or match.group(2)):
        minutes = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
        if minutes > 0:
            return int(now + minutes * 60)
    raise ValueError(f"not a duration or time: {text!r} (try 30m, 1h30m, 21:00, phase)")


def clock(timestamp):
    return time.strftime("%H:%M", time.localtime(timestamp)) if timestamp else "—"


def describe_until(until):
    return "until resumed" if not until else f"until {clock(until)}"


# --- status ----------------------------------------------------------------


def local_status():
    """What the daemon would report, computed here when it is not running."""
    from .curve import Schedule
    cfg = config_module.load()
    schedule = Schedule(cfg)
    now = time.time()
    forecast = weather.load_cache()
    cloud_at = lambda t: (forecast.cloud_at(t) if cfg.weather_enabled and forecast
                          and forecast.usable(now, schedule.latitude, schedule.longitude,
                                              cfg.weather_max_age_hours) else None)
    decision = policy.decide(schedule, policy.State(), now, cloud_at(now), cfg)
    when, level = policy.next_change(schedule, policy.State(), now, cloud_at, cfg)
    sun = schedule.sun(schedule.local_date(now))
    return {
        "level": decision.level, "mode": "auto", "phase": decision.phase,
        "factor": decision.factor,
        "cloud": -1.0 if decision.cloud is None else decision.cloud,
        "until": 0, "next_change": int(when or 0), "next_level": int(level or 0),
        "sunrise": int(sun.rise.timestamp()) if sun.rise else 0,
        "sunset": int(sun.set.timestamp()) if sun.set else 0, "polar": sun.polar or "",
        "latitude": schedule.latitude, "longitude": schedule.longitude,
        "location_source": schedule.location_source, "weather_enabled": cfg.weather_enabled,
        "weather_updated": int(forecast.fetched_at) if forecast else 0,
        "weather_error": "", "outputs": [], "config_error": "", "zone": cfg.zone_name(),
    }


def print_status(s, running):
    if s.get("config_error"):
        print(f"settings error: {s['config_error']}")
    if "level" not in s:
        return
    modes = {"auto": "automatic", "override": "manual", "paused": "paused",
             "preview": "preview"}
    line = f"Brightness {s['level']} %  ({modes.get(s['mode'], s['mode'])}, {s['phase']}"
    if s["mode"] in ("override", "paused"):
        line += f", {describe_until(s.get('until'))}"
    print(line + ")")
    if s["mode"] != "auto":
        print(f"  schedule  {s.get('scheduled', '—')} %")
    if s.get("next_change"):
        print(f"  next      {s['next_level']} % at {clock(s['next_change'])}")
    if s.get("polar"):
        print(f"  sun       polar {s['polar']}")
    else:
        print(f"  sun       rise {clock(s.get('sunrise'))}, set {clock(s.get('sunset'))}")
    source = "settings" if s.get("location_source") == "config" else "time zone"
    print(f"  location  {s.get('latitude', 0):.4f}, {s.get('longitude', 0):.4f} "
          f"(from {source})")
    if s.get("weather_enabled"):
        if s.get("cloud", -1) >= 0:
            change = round((s.get("factor", 1) - 1) * 100)
            print(f"  weather   cloud cover {s['cloud']:.0f} %, {change:+d} % "
                  f"(updated {clock(s.get('weather_updated'))})")
        else:
            print(f"  weather   no forecast{': ' + s['weather_error'] if s.get('weather_error') else ''}")
    else:
        print("  weather   off")
    for o in s.get("outputs", []):
        state = "" if o["state"] == "ok" else f", {o['state']}"
        applied = "?" if o["applied"] < 0 else f"{o['applied']} %"
        extra = "" if o["enabled"] else ", disabled"
        if o.get("manual", -1) >= 0:
            extra += ", held by brightness keys"
        if not o["awake"]:
            extra += ", asleep"
        print(f"  display   {o['label']}: {applied}{state}{extra}  [{o['method']}]")
    if not running:
        print("  daemon    NOT RUNNING (systemctl --user start ddc-sun-dimmer)")


def cmd_status(args):
    from .client import Client, DaemonUnavailable
    try:
        status, running = Client().status(), True
    except DaemonUnavailable:
        status, running = local_status(), False
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2, default=str))
    else:
        print_status(status, running)
    return 0


# --- direct hardware access ------------------------------------------------


def direct_outputs(cfg, ddc_only=False, bus=None):
    """(label, actuator) pairs, for writing without the daemon."""
    from .backends.backlight import BacklightActuator
    from .backends.ddc import DdcActuator
    found = []
    connectors = displays.list_connectors()
    if cfg.ddc.enabled or bus is not None:
        for c in connectors:
            if not c.connected or c.internal or c.bus is None:
                continue
            wanted = bus if bus is not None else (cfg.ddc.bus.strip() or None)
            if wanted is not None and str(c.bus) != str(wanted):
                continue
            settings = cfg.ddc_for(c.key)
            repeats = os.environ.get("DIM_REPEATS", "")
            if repeats.isdigit() and int(repeats) > 0:
                settings.repeats = int(repeats)
            found.append((c.label, DdcActuator(c.bus, settings)))
    if cfg.backlight_enabled and not ddc_only:
        device = displays.pick_backlight(displays.list_backlights(connectors=connectors),
                                         cfg.backlight_device)
        if device:
            found.append((f"built-in ({device.name})",
                          BacklightActuator(device, cfg.backlight_method)))
    return found


def write_direct(level, vcp=None, bus=None):
    from .backends.ddc import VCP_BRIGHTNESS
    cfg = config_module.load()
    outputs = direct_outputs(cfg, ddc_only=vcp is not None, bus=bus)
    if not outputs:
        print("no controllable display found", file=sys.stderr)
        return 1
    status = 0
    name = "contrast" if vcp else "brightness"
    for label, actuator in outputs:
        if actuator.kind == "ddc":
            delivered, total = actuator.set_vcp(vcp or VCP_BRIGHTNESS, level)
            ok = delivered > 0
            detail = f" ({delivered}/{total} writes went through)"
        else:
            ok, detail = actuator.write(level), ""
        print(f"{label}: {name} {level} %{detail}" if ok
              else f"{label}: could not set {name} {level} %", file=None if ok else sys.stderr)
        status = status or (0 if ok else 1)
    return status


# --- commands --------------------------------------------------------------


def with_daemon(action, fallback=None):
    from .client import Client, DaemonError, DaemonUnavailable
    try:
        return action(Client())
    except DaemonUnavailable as error:
        if fallback is not None:
            return fallback()
        print(f"{error} (systemctl --user start ddc-sun-dimmer)", file=sys.stderr)
        return 1
    except DaemonError as error:
        print(error, file=sys.stderr)
        return 1


def cmd_set(args):
    level = clamp_level(args.level)
    if args.forever:
        until = policy.FOREVER
    elif args.until:
        until = parse_when(args.until)
    else:
        until = policy.NEXT_PHASE

    def act(client):
        client.set_level(level, until)
        s = client.status()
        print(f"brightness {level} % {describe_until(s.get('until'))}")
        return 0
    return with_daemon(act, fallback=lambda: write_direct(level))


def cmd_adjust(args):
    def act(client):
        client.adjust(args.delta)
        print(f"brightness {client.status()['level']} %")
        return 0
    return with_daemon(act)


def cmd_pause(args):
    until = parse_when(args.when or "")

    def act(client):
        client.pause(until)
        s = client.status()
        print(f"paused at {s['level']} % {describe_until(s.get('until'))}")
        return 0
    return with_daemon(act)


def cmd_resume(args):
    def act(client):
        client.resume()
        print(f"back to the schedule: {client.status()['level']} %")
        return 0
    return with_daemon(act)


def cmd_contrast(args):
    from .backends.ddc import VCP_CONTRAST
    return write_direct(clamp_level(args.value), vcp=VCP_CONTRAST, bus=args.bus)


def cmd_displays(args):
    connectors = displays.list_connectors()
    for c in connectors:
        if not c.connected:
            continue
        bus = f"/dev/i2c-{c.bus}" if c.bus is not None else "no DDC bus"
        role = "built-in" if c.internal else bus
        print(f"{c.key:24} {c.label:36} {role:14} panel {'on' if c.awake() else 'asleep'}")
    for device in displays.list_backlights(connectors=connectors):
        owner = device.connector.short if device.connector else "?"
        print(f"{'internal':24} backlight {device.name:26} {device.type:9} "
              f"max {device.max_brightness} ({owner})")
    return 0


def cmd_weather(args):
    from .client import Client, DaemonUnavailable
    if args.refresh:
        return with_daemon(lambda c: (c.refresh_weather(), print("refresh requested"), 0)[2])
    try:
        fetched_at, points = Client().forecast()
    except DaemonUnavailable:
        cached = weather.load_cache()
        fetched_at, points = (cached.fetched_at, list(zip(cached.times, cached.cloud))) \
            if cached else (0, [])
    if not points:
        print("no forecast yet")
        return 1
    print(f"{weather.PROVIDER}, fetched {time.strftime('%d.%m %H:%M', time.localtime(fetched_at))}")
    now = time.time()
    for timestamp, cloud in points:
        if now - 3600 <= timestamp <= now + 24 * 3600:
            bar = "#" * round(cloud / 5)
            print(f"  {time.strftime('%d.%m %H:%M', time.localtime(timestamp))}  "
                  f"{cloud:5.0f} %  {bar}")
    return 0


def cmd_profile(args):
    from .curve import Schedule
    cfg = config_module.load()
    schedule = Schedule(cfg)
    date = dt.date.fromisoformat(args.date) if args.date else schedule.local_date(time.time())
    sun = schedule.sun(date)
    print(f"{date}  rise {sun.rise:%H:%M}  set {sun.set:%H:%M}" if sun.rise
          else f"{date}  polar {sun.polar}")
    for timestamp, level in schedule.profile(date, args.step):
        local = dt.datetime.fromtimestamp(timestamp, schedule.tz)
        if local.date() != date:
            continue
        print(f"  {local:%H:%M}  {clamp_level(level):3d} %  {'#' * round(level / 2)}")
    return 0


def cmd_config(args):
    path = config_module.paths.config_file()
    if args.path:
        print(path)
        return 0
    try:
        cfg = config_module.load()
    except config_module.ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    if args.print:
        print(config_module.render(cfg), end="")
    else:
        lat, lon, source = cfg.location()
        print(f"{path}: OK ({lat:.4f}, {lon:.4f} from "
              f"{'settings' if source == 'config' else 'time zone'})")
    return 0


def cmd_daemon(args):
    stream = os.environ.get("JOURNAL_STREAM")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s" if stream else "%(asctime)s %(message)s",
        datefmt="%H:%M:%S")
    from .daemon import Daemon
    return Daemon(dry_run=args.dry_run, tray=not args.no_tray).run()


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(
        prog="ddc-sun-dimmer",
        description="Display brightness that follows the sun (DDC/CI and laptop backlight).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("daemon", help="run the daemon (what the systemd unit starts)")
    p.add_argument("--dry-run", action="store_true", help="log writes instead of doing them")
    p.add_argument("--no-tray", action="store_true", help="never show a tray icon")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("status", help="current level and what comes next")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("set", help="set a manual level")
    p.add_argument("level", type=int)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--until", metavar="WHEN", help="30m, 1h, 21:00 (default: next phase)")
    group.add_argument("--forever", action="store_true", help="until resumed")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("adjust", help="brighter or dimmer by DELTA points")
    p.add_argument("delta", type=int)
    p.set_defaults(func=cmd_adjust)

    p = sub.add_parser("pause", help="freeze the schedule")
    p.add_argument("when", nargs="?", help="30m, 1h30m, 21:00, phase (default: until resumed)")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="back to the schedule")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("contrast", help="set DDC contrast once (never scheduled)")
    p.add_argument("value", type=int)
    p.add_argument("--bus", type=int)
    p.set_defaults(func=cmd_contrast)

    p = sub.add_parser("displays", help="list detected displays")
    p.set_defaults(func=cmd_displays)

    p = sub.add_parser("weather", help="show the cloud forecast")
    p.add_argument("--refresh", action="store_true", help="download a fresh forecast now")
    p.set_defaults(func=cmd_weather)

    p = sub.add_parser("profile", help="print the curve for a day")
    p.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today)")
    p.add_argument("--step", type=int, default=30, help="minutes between rows")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("config", help="check the settings file")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--path", action="store_true", help="print its location")
    group.add_argument("--print", action="store_true", help="print it in canonical form")
    p.set_defaults(func=cmd_config)
    return parser


def ddc_main(argv):
    if argv and argv[0] == "--status":          # version 1 spelling
        argv = ["status"] + argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except (ValueError, config_module.ConfigError) as error:
        print(error, file=sys.stderr)
        return 1


def dim_main(argv):
    import argparse
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(DIM_HELP, end="")
        return 0
    first, rest = argv[0], argv[1:]
    try:
        if first in ("status", "-s", "--status"):
            return cmd_status(argparse.Namespace(json=False))
        if first in ("pause", "stop", "--pause"):
            return cmd_pause(argparse.Namespace(when=" ".join(rest)))
        if first in ("resume", "start", "--resume"):
            return cmd_resume(argparse.Namespace())
        if first in ("-c", "--contrast"):
            if not rest or not rest[0].isdigit():
                print("dim -c needs a value from 1 to 100", file=sys.stderr)
                return 1
            return cmd_contrast(argparse.Namespace(value=int(rest[0]), bus=None))
        if re.fullmatch(r"[+-]\d+", first):
            return cmd_adjust(argparse.Namespace(delta=int(first)))
        if first.isdigit() and 1 <= int(first) <= 100:
            return cmd_set(argparse.Namespace(level=int(first), until=None, forever=False))
    except (ValueError, config_module.ConfigError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"not understood: {first}\nexpected 1-100, +N, -N, -c N, status, pause, resume "
          "(dim --help)", file=sys.stderr)
    return 1


def main(argv=None):
    program = os.path.basename(sys.argv[0])
    argv = sys.argv[1:] if argv is None else argv
    if program == "dim":
        return dim_main(argv)
    return ddc_main(argv)
