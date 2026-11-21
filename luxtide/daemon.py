"""The daemon: schedule, displays, weather, D-Bus API and tray in one process.

Version 1 was a timer starting a fresh Python every minute, a oneshot
service and a bash watcher, talking through files. A resident process
replaces all three, keeps its state in memory, wakes only when there is
something to do, and can be asked things over D-Bus.
"""

import logging
import os
import pathlib
import shutil
import signal
import sys
import threading
import time

from gi.repository import Gio, GLib

from . import (BUS_NAME, INTERFACE, OBJECT_PATH, __version__, clamp_level,
               config as config_module, displays, locate, paths, policy, sdnotify,
               weather)
from .backends.backlight import DESKTOP_NAMES, BacklightActuator, _Bus, session_idle_ms
from .backends.ddc import DdcActuator
from .curve import Schedule
from .outputs import Output

log = logging.getLogger("luxtide")

TICK_SECONDS = 30
POLL_START = 0.5       # just started, maybe before the desktop: catch the panel lighting up
START_WINDOW = 30
POLL_FAST = 2          # panel state while dimmed: reaction time matters
POLL_SLOW = 20         # nothing to correct after a wake-up
POLL_MANUAL = 3        # readable backlights: notice the brightness keys
POWER_SCHEMA = "org.gnome.settings-daemon.plugins.power"
SENSOR_PROXY = "net.hadess.SensorProxy"
RESCAN_SECONDS = 30
WEATHER_CHECK_SECONDS = 300
WEATHER_LOOKAHEAD = 3 * 3600
FOLLOW_START_DELAY = 20       # let the session settle before asking where we are
FOLLOW_NETWORK_DELAY = 15     # a new network needs a moment before Wi-Fi scans help
FOLLOW_MIN_INTERVAL = 600
ERROR = "io.github.MikolajQ.Luxtide.Error"

INTERFACE_XML = f"""
<node>
  <interface name="{INTERFACE}">
    <method name="GetStatus">
      <arg name="status" type="a{{sv}}" direction="out"/>
    </method>
    <method name="SetLevel">
      <arg name="level" type="i" direction="in"/>
      <arg name="until" type="x" direction="in"/>
    </method>
    <method name="Adjust">
      <arg name="delta" type="i" direction="in"/>
    </method>
    <method name="Step">
      <arg name="direction" type="i" direction="in"/>
    </method>
    <method name="Pause">
      <arg name="until" type="x" direction="in"/>
    </method>
    <method name="Resume"/>
    <method name="Preview">
      <arg name="level" type="i" direction="in"/>
    </method>
    <method name="EndPreview"/>
    <method name="Reload"/>
    <method name="RefreshWeather"/>
    <method name="GetForecast">
      <arg name="fetched_at" type="d" direction="out"/>
      <arg name="points" type="a(xd)" direction="out"/>
    </method>
    <signal name="Changed">
      <arg name="status" type="a{{sv}}"/>
    </signal>
  </interface>
</node>
"""


class GLibScheduler:
    """Timers and main-thread calls for Output, backed by GLib."""

    def __init__(self):
        self.active = set()

    def timeout(self, seconds, callback):
        box = []

        def fire():
            self.active.discard(box[0])
            callback()
            return False

        box.append(GLib.timeout_add(int(seconds * 1000), fire))
        self.active.add(box[0])
        return box[0]

    def cancel(self, handle):
        if handle in self.active:
            self.active.discard(handle)
            GLib.source_remove(handle)

    def call_main(self, callback, *args):
        def run():
            callback(*args)
            return False
        GLib.idle_add(run)


def launcher_path(name):
    """A sibling executable of the running one, else whatever is on PATH."""
    here = pathlib.Path(os.path.realpath(sys.argv[0])).parent
    candidate = here / name
    if candidate.exists():
        return str(candidate)
    return shutil.which(name) or name


def spawn_detached(argv):
    """Start a program outside our cgroup, so a daemon restart does not kill it."""
    if shutil.which("systemd-run"):
        argv = ["systemd-run", "--user", "--collect", "--quiet", "--", *argv]
    try:
        process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_SILENCE
                                     | Gio.SubprocessFlags.STDERR_SILENCE)
        process.wait_async(None, None, None)
    except GLib.Error as error:
        log.warning("could not start %s: %s", argv[-1], error.message)


class Daemon:
    def __init__(self, dry_run=False, tray=True):
        self.dry_run = dry_run
        self.tray_allowed = tray
        self.scheduler = GLibScheduler()
        self.loop = GLib.MainLoop()
        self.exit_code = 0

        self.cfg = None
        self.schedule = None
        self.config_error = ""
        self._load_config()

        self.state = self._load_state()
        self.forecast = weather.load_cache()
        self.weather_error = ""
        self.weather_fetching = False
        self.weather_retry_at = 0.0
        self.weather_fails = 0

        self.outputs = {}
        self.decision = None
        self._next_change = None          # (computed for, until, (time, level))
        self._signature = None
        self._status = {}
        self._poll_source = None
        self._poll_current = None
        self._in_poll = False
        self._reload_source = None
        self._preview_source = None
        self._zone = self.cfg.zone_name() if self.cfg else None
        self._follow_was_on = self.cfg.location_follow if self.cfg else False
        self._idle_bus = None
        self._locator = None
        self._last_locate = float("-inf")
        self._follow_source = None
        self._follow_error = ""
        self.tray = None
        self.session = None
        self.system = None
        self.started = time.monotonic()
        self._name_watches = []
        self._power_settings = None
        self.light_sensor = None          # unknown until asked
        self._sensor_asked = False

    # --- configuration and state ------------------------------------------

    def _load_config(self):
        try:
            cfg = config_module.load()
            schedule = Schedule(cfg)
        except config_module.ConfigError as error:
            self.config_error = str(error)
            log.error("settings: %s", error)
            if self.cfg is None:
                # Keep running so the error is visible in the tray and the
                # settings window, where it can be fixed.
                self.cfg = config_module.Config()
            return False
        self.cfg, self.schedule, self.config_error = cfg, schedule, ""
        self._next_change = None
        return True

    def reload_config(self):
        if not self._load_config():
            self.publish(force=True)
            return
        log.info("settings reloaded")
        was_on = self._follow_was_on
        self._zone = self.cfg.zone_name()
        self.rescan()
        self._sync_tray()
        self.weather_check()
        if self.cfg.location_follow and not was_on:
            self.follow_soon(1, "turned on")
        self._follow_was_on = self.cfg.location_follow
        self.tick()

    def _state_file(self):
        return paths.runtime_dir() / "state.json"

    def _load_state(self):
        try:
            return policy.State.from_json(self._state_file().read_text(), time.time())
        except OSError:
            return policy.State()

    def _save_state(self):
        try:
            paths.atomic_write(self._state_file(), self.state.to_json())
        except OSError as error:
            log.warning("could not save state: %s", error)
        self._next_change = None

    def _check_timezone(self):
        """Laptops travel: follow a system time zone change without a restart."""
        if self.cfg.timezone:
            return
        zone = self.cfg.zone_name()
        if zone != self._zone:
            log.info("time zone changed to %s", zone)
            self._zone = zone
            self.reload_config()

    # --- the decision ------------------------------------------------------

    def weather_blocked(self):
        """The weather needs real coordinates: the reference city of a time zone
        can be hundreds of kilometres away, under a different sky."""
        return (self.cfg.weather_enabled and self.schedule is not None
                and self.schedule.location_source != "config")

    def cloud_at(self, timestamp, now=None):
        if not self.cfg.weather_enabled or self.forecast is None or self.schedule is None:
            return None
        if self.weather_blocked():
            return None
        now = now or time.time()
        if not self.forecast.usable(now, self.schedule.latitude, self.schedule.longitude,
                                    self.cfg.weather_max_age_hours):
            return None
        return self.forecast.cloud_at(timestamp)

    def level_now(self):
        """Fresh decision for wake-up bursts, which run between ticks."""
        if self.schedule is None:
            return None
        now = time.time()
        self.state.expire(now)
        return policy.decide(self.schedule, self.state, now, self.cloud_at(now),
                             self.cfg).level

    def tick(self, *_):
        now = time.time()
        if self.state.expire(now):
            log.info("pause or manual level ended, back to the schedule")
            self._save_state()
        self._check_timezone()
        if self.schedule is None:
            self.decision = None
            self.publish()
            return True
        decision = policy.decide(self.schedule, self.state, now, self.cloud_at(now), self.cfg)
        previous = self.decision
        if previous is None or (decision.level, decision.mode) != (previous.level, previous.mode):
            weather_note = ""
            if decision.factor < 0.995:
                weather_note = f", cloud {decision.cloud:.0f}% x{decision.factor:.2f}"
            log.info("level %d%% (%s, %s%s)", decision.level, decision.mode,
                     decision.phase, weather_note)
        self.decision = decision
        monotonic = time.monotonic()
        for output in self.outputs.values():
            output.ensure(decision.level, monotonic)
        self.publish()
        return True

    def next_change(self, now):
        cached = self._next_change
        if cached and cached[0] <= now and (cached[1] is None or now < cached[1]):
            return cached[2]
        when, level = policy.next_change(self.schedule, self.state, now,
                                         lambda t: self.cloud_at(t, now), self.cfg)
        self._next_change = (now, when, (when, level))
        return when, level

    # --- displays ----------------------------------------------------------

    def rescan(self, initial=False):
        cfg = self.cfg
        found = {}
        connectors = displays.list_connectors()
        if cfg.ddc.enabled:
            for c in connectors:
                if not c.connected or c.internal or c.bus is None:
                    continue
                if cfg.ddc.bus and str(c.bus) != cfg.ddc.bus.strip():
                    continue
                found[c.key] = ("ddc", c, c.label, c.awake)
        if cfg.backlight_enabled:
            device = displays.pick_backlight(
                displays.list_backlights(connectors=connectors), cfg.backlight_device)
            if device is not None:
                label = f"Built-in display ({device.name})"
                found["internal"] = ("backlight", device, label, device.awake)

        for key in list(self.outputs):
            output = self.outputs[key]
            gone = key not in found
            moved = (not gone and output.kind == "ddc"
                     and found[key][1].bus != output.actuator.bus)
            if gone or moved:
                log.info("%s %s", output.label, "disconnected" if gone else "moved")
                output.close()
                del self.outputs[key]

        for key, (kind, thing, label, awake) in found.items():
            if key in self.outputs:
                output = self.outputs[key]
                output.display = cfg.display(key)
                if kind == "ddc":
                    output.settings = cfg.ddc_for(key)
                    output.actuator.settings = output.settings
                elif output.actuator.requested != cfg.backlight_method:
                    output.actuator.requested = cfg.backlight_method
                    self._reselect(output)
                continue
            if kind == "ddc":
                settings = cfg.ddc_for(key)
                actuator = DdcActuator(thing.bus, settings, dry_run=self.dry_run)
            else:
                settings = None
                actuator = BacklightActuator(thing, cfg.backlight_method, dry_run=self.dry_run)
                actuator.select()
            output = Output(key, label, actuator, cfg.display(key), settings, awake,
                            self.scheduler)
            self.outputs[key] = output
            if kind == "backlight" and not self.dry_run:
                actuator.watch(self.scheduler,
                               lambda change, o=output: self._user_changed(o, change))
            log.info("controlling %s: %s%s", label, actuator.describe(),
                     "" if output.display.enabled else " (disabled in settings)")
            if output.awake:
                output.on_wake(self.level_now)
        if not self.outputs and initial:
            log.warning("no controllable display found")
        self._check_ambient()
        self._arm_poll()
        return True

    def _reselect(self, output):
        """Pick the backlight method again; re-assert the level on a change."""
        before = output.actuator.method
        method = output.actuator.select()
        if method is not before:
            log.info("%s: now %s", output.label, output.actuator.describe())
            if output.awake or output.events:
                output.on_wake(self.level_now)
            self._check_ambient()
            self._arm_poll()
            self.tick()                       # write now, not at the next tick

    def _watch_desktop(self):
        """GNOME Shell or Plasma may come up after us (we start before the
        desktop to beat the brightness it restores), or restart: follow
        their interfaces coming and going instead of polling for them."""
        def changed(connection, name, owner=None):
            log.debug("%s %s", name, "appeared" if owner else "vanished")
            for output in list(self.outputs.values()):
                if output.kind == "backlight":
                    self._reselect(output)
        for name in DESKTOP_NAMES:
            self._name_watches.append(Gio.bus_watch_name_on_connection(
                self.session, name, Gio.BusNameWatcherFlags.NONE,
                lambda c, n, o: changed(c, n, o), lambda c, n: changed(c, n)))

    def _user_changed(self, output, change):
        """The desktop reported the brightness keys or its slider."""
        if self.schedule is None or self.outputs.get(output.key) is not output:
            return
        if not self.cfg.backlight_follow_manual:
            output.applied = None             # put our level back
            self.tick()
            return
        level = output.user_change(change, self.cfg.step)
        if level is None:
            return
        until = policy.resolve_until(policy.NEXT_PHASE, time.time(), self.schedule)
        output.hold_manual(level, until)
        self.tick()

    # --- GNOME's own automatic brightness ------------------------------------

    def _uses_shell(self):
        return any(o.kind == "backlight" and o.actuator.method is not None
                   and o.actuator.method.name == "gnome-shell" for o in self.outputs.values())

    def _check_ambient(self):
        """GNOME's automatic brightness, with a light sensor, sets the same
        target as we do through the shell; two automatic controllers would
        fight over it. It is turned off while we control the panel, and the
        settings window says so. Without a sensor it does nothing, and the
        setting is left alone."""
        if self.dry_run or self.session is None or not self._uses_shell():
            return
        if self.light_sensor is None:
            if self.system is not None and not self._sensor_asked:
                self._ask_sensor()
            return
        if not self.light_sensor:
            return
        if self._power_settings is None:
            source = Gio.SettingsSchemaSource.get_default()
            if source is None or source.lookup(POWER_SCHEMA, True) is None:
                return
            self._power_settings = Gio.Settings.new(POWER_SCHEMA)
            self._power_settings.connect("changed::ambient-enabled",
                                         lambda *_: self._check_ambient())
        if self._power_settings.get_boolean("ambient-enabled"):
            self._power_settings.set_boolean("ambient-enabled", False)
            Gio.Settings.sync()
            log.info("turned off GNOME's automatic brightness: its light sensor "
                     "would fight Luxtide over the same setting")

    def _ask_sensor(self):
        self._sensor_asked = True

        def answered(connection, result):
            try:
                self.light_sensor = bool(connection.call_finish(result).unpack()[0])
            except GLib.Error as error:
                log.debug("no light sensor service: %s", error.message)
                self.light_sensor = False
            log.debug("ambient light sensor: %s", "yes" if self.light_sensor else "no")
            self._check_ambient()
            self.publish()
        self.system.call(SENSOR_PROXY, "/net/hadess/SensorProxy",
                         "org.freedesktop.DBus.Properties", "Get",
                         GLib.Variant("(ss)", (SENSOR_PROXY, "HasAmbientLight")),
                         GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 5000, None,
                         answered)

    def _poll_interval(self):
        if time.monotonic() - self.started < START_WINDOW:
            return POLL_START
        level = self.decision.level if self.decision else None
        interval = POLL_SLOW
        for output in self.outputs.values():
            if level is not None and output.dimmed(level):
                return POLL_FAST
            if output.wants_fast_poll() and (self.cfg.backlight_follow_manual
                                             or output.events):
                interval = POLL_MANUAL
        return interval

    def _arm_poll(self):
        """(Re)start the panel poll, keeping the running timer when it fits."""
        if self._in_poll:
            return                        # _poll_timer re-arms after the poll
        interval = self._poll_interval()
        if self._poll_source and interval == self._poll_current:
            return
        if self._poll_source:
            GLib.source_remove(self._poll_source)
        self._poll_current = interval
        if interval >= 1:
            self._poll_source = GLib.timeout_add_seconds(int(interval), self._poll_timer)
        else:
            self._poll_source = GLib.timeout_add(int(interval * 1000), self._poll_timer)

    def _poll_timer(self):
        self._in_poll = True
        try:
            self.poll()
        finally:
            self._in_poll = False
        if self._poll_interval() == self._poll_current:
            return True                   # one timer for the whole life, not one per poll
        self._poll_source = None
        self._arm_poll()
        return False

    def poll(self):
        changed = False
        for output in list(self.outputs.values()):
            event = output.poll_awake()
            if event == "wake":
                log.info("%s woke up", output.label)
                output.on_wake(self.level_now)
                changed = True
            elif event == "sleep":
                log.info("%s went to sleep", output.label)
                output.on_sleep()
                changed = True
        idle = lambda: session_idle_ms(self._idle())
        for output in self.outputs.values():
            if output.events and output.verify(idle):
                changed = True
        if self.cfg.backlight_follow_manual and self.schedule is not None:
            readable = [o for o in self.outputs.values()
                        if o.actuator.readable and not o.events]
            if readable:
                for output in readable:
                    level = output.check_manual(idle)
                    if level is not None:
                        until = policy.resolve_until(policy.NEXT_PHASE, time.time(),
                                                     self.schedule)
                        output.hold_manual(level, until)
                        changed = True
        if changed:
            self.tick()

    def _idle(self):
        if self._idle_bus is None:
            try:
                self._idle_bus = _Bus(system=False)
            except Exception:
                return None
        return self._idle_bus

    # --- weather -----------------------------------------------------------

    def weather_relevant(self, now):
        """Is there daylight now or within a few hours? At night cloud is moot."""
        for step in range(0, WEATHER_LOOKAHEAD + 1, 900):
            if self.schedule.daylight_weight(self.schedule.base(now + step)) > 0:
                return True
        return False

    def weather_check(self, force=False):
        if not self.cfg.weather_enabled or self.schedule is None or self.weather_fetching:
            return True
        if self.weather_blocked():
            return True
        now = time.time()
        if not force:
            if now < self.weather_retry_at:
                return True
            lat, lon = self.schedule.latitude, self.schedule.longitude
            fc = self.forecast
            if fc and fc.covers(lat, lon) and \
                    now - fc.fetched_at < self.cfg.weather_refresh_minutes * 60:
                return True
            if not self.weather_relevant(now):
                return True
        self.weather_fetching = True
        lat, lon = self.schedule.latitude, self.schedule.longitude
        threading.Thread(target=self._fetch_weather, args=(lat, lon), daemon=True,
                         name="weather").start()
        return True

    def _fetch_weather(self, lat, lon):
        try:
            result, error = weather.fetch(lat, lon), None
        except weather.WeatherError as failure:
            result, error = None, str(failure)
        GLib.idle_add(self._weather_done, result, error)

    def _weather_done(self, forecast, error):
        self.weather_fetching = False
        now = time.time()
        if forecast is not None:
            self.forecast = forecast
            self.weather_error = ""
            self.weather_fails = 0
            self.weather_retry_at = 0.0
            try:
                weather.save_cache(forecast)
            except OSError as failure:
                log.debug("could not cache forecast: %s", failure)
            cloud = forecast.cloud_at(now)
            log.info("forecast updated (%s): cloud cover now %s", weather.PROVIDER,
                     "unknown" if cloud is None else f"{cloud:.0f}%")
        else:
            self.weather_fails += 1
            delay = min(900 * 2 ** (self.weather_fails - 1),
                        self.cfg.weather_refresh_minutes * 60)
            self.weather_retry_at = now + delay
            self.weather_error = error
            log.warning("forecast download failed: %s (next try in %d min)", error,
                        delay // 60)
        self._next_change = None
        self.tick()
        return False

    # --- following the computer's location ------------------------------------

    def follow_soon(self, delay, reason):
        """Ask GeoClue where we are in `delay` seconds, when following is on."""
        if not self.cfg.location_follow:
            return
        if self._follow_source:
            GLib.source_remove(self._follow_source)

        def fire():
            self._follow_source = None
            self.follow_check(reason)
            return False
        self._follow_source = GLib.timeout_add_seconds(delay, fire)

    def follow_check(self, reason, force=False):
        if not self.cfg.location_follow or self.config_error or self._locator is not None:
            return
        now = time.monotonic()
        if not force and now - self._last_locate < FOLLOW_MIN_INTERVAL:
            return
        self._last_locate = now
        log.debug("asking the location service (%s)", reason)
        self._locator = locate.GeoClueLocator(self._follow_done, timeout=45, good_enough=100)
        self._locator.start()

    def _follow_done(self, fix, error):
        self._locator = None
        if error:
            if error != self._follow_error:
                log.warning("location not updated: %s", error)
            self._follow_error = error
            self.publish()
            return
        self._follow_error = ""
        current = None
        if self.cfg.latitude is not None and self.cfg.longitude is not None:
            current = (self.cfg.latitude, self.cfg.longitude)
        if not locate.should_adopt(current, fix):
            if fix.accuracy > 3000:
                log.info("location service only knows roughly where we are (%s); "
                         "keeping the saved location", locate.accuracy_text(fix.accuracy))
            else:
                log.debug("location unchanged (%s)", locate.accuracy_text(fix.accuracy))
            self.publish()
            return
        self.cfg.set_location(fix.latitude, fix.longitude, "geoclue", accuracy=fix.accuracy)
        log.info("location updated by the location service: %.4f, %.4f (%s)",
                 fix.latitude, fix.longitude, locate.accuracy_text(fix.accuracy))
        try:
            config_module.save(self.cfg)       # the file monitor reloads it
        except OSError as error:
            log.warning("could not save the new location: %s", error)

    # --- status ------------------------------------------------------------

    def status(self):
        """Everything clients show, as plain Python values."""
        now = time.time()
        s = {"version": __version__, "dry_run": self.dry_run,
             "config_error": self.config_error, "now": int(now)}
        cfg = self.cfg
        s["pause_presets"] = list(cfg.tray_pause_presets)
        s["show_level"] = cfg.tray_show_level
        s["weather_enabled"] = cfg.weather_enabled
        s["weather_error"] = self.weather_error
        s["weather_blocked"] = self.weather_blocked()
        s["location_follow"] = cfg.location_follow
        s["location_method"] = cfg.location_method
        s["location_place"] = cfg.location_place
        s["location_accuracy"] = cfg.location_accuracy
        s["location_updated"] = cfg.location_updated
        s["location_error"] = self._follow_error
        s["weather_updated"] = int(self.forecast.fetched_at) if self.forecast else 0
        if self.schedule is not None:
            sun = self.schedule.sun(self.schedule.local_date(now))
            s["sunrise"] = int(sun.rise.timestamp()) if sun.rise else 0
            s["sunset"] = int(sun.set.timestamp()) if sun.set else 0
            s["polar"] = sun.polar or ""
            s["latitude"] = self.schedule.latitude
            s["longitude"] = self.schedule.longitude
            s["location_source"] = self.schedule.location_source
            s["next_phase"] = int(self.schedule.next_phase_change(now) or 0)
        d = self.decision
        if d is not None:
            s.update(level=d.level, scheduled=d.scheduled, base=float(d.base),
                     factor=float(d.factor), cloud=-1.0 if d.cloud is None else float(d.cloud),
                     mode=d.mode, until=int(d.until or 0), phase=d.phase)
            when, level = self.next_change(now)
            s["next_change"] = int(when or 0)
            s["next_level"] = int(level or 0)
        outputs = []
        for o in self.outputs.values():
            outputs.append({
                "key": o.key, "label": o.label, "kind": o.kind,
                "method": o.actuator.describe(), "enabled": o.display.enabled,
                "awake": o.awake, "applied": -1 if o.applied is None else o.applied,
                "target": -1 if o.last_target is None else o.last_target,
                "state": {True: "ok", False: "failed", None: "unknown"}[o.ok],
                "manual": o.manual_hold.level if o.manual_hold else -1,
                "manual_until": int(o.manual_hold.until or 0) if o.manual_hold else 0,
                "note": getattr(o.actuator, "note", "") or "",
            })
        s["outputs"] = outputs
        # A level set with the brightness keys holds on that display only, so
        # the decided level above is not what the screen shows.
        enabled = [o for o in self.outputs.values() if o.display.enabled]
        held = [o for o in enabled if o.manual_hold is not None]
        if held and d is not None:
            s["keys_level"] = held[0].manual_hold.level
            s["keys_until"] = int(held[0].manual_hold.until or 0)
            s["keys_all"] = len(held) == len(enabled)
        else:
            s["keys_level"], s["keys_until"], s["keys_all"] = -1, 0, False
        if d is not None:
            s["shown"] = self._shown_level()
        s["step"] = cfg.step
        # GNOME's sensor-driven brightness is kept off while we drive the panel.
        s["gnome_ambient_off"] = bool(self.light_sensor) and self._uses_shell()
        return s

    @staticmethod
    def to_variant(status):
        def wrap(value):
            if isinstance(value, bool):
                return GLib.Variant("b", value)
            if isinstance(value, int):
                return GLib.Variant("x", value)
            if isinstance(value, float):
                return GLib.Variant("d", value)
            if isinstance(value, str):
                return GLib.Variant("s", value)
            if isinstance(value, list):
                return GLib.Variant("ax", value)
            raise TypeError(f"cannot send {value!r}")

        result = {}
        for key, value in status.items():
            if key == "outputs":
                result[key] = GLib.Variant("aa{sv}", [{k: wrap(v) for k, v in item.items()}
                                                      for item in value])
            else:
                result[key] = wrap(value)
        return GLib.Variant("a{sv}", result)

    def publish(self, force=False):
        status = self.status()
        self._status = status
        # Cloud cover is interpolated continuously, so the raw floats change at
        # every tick; only a visible difference is worth a signal and a menu.
        precision = {"cloud": 0, "base": 1, "factor": 2}
        signature = {k: round(v, precision.get(k, 4)) if isinstance(v, float) else v
                     for k, v in status.items() if k != "now"}
        if signature == self._signature and not force:
            return
        self._signature = signature
        if self.session is not None:
            try:
                self.session.emit_signal(None, OBJECT_PATH, INTERFACE, "Changed",
                                         GLib.Variant.new_tuple(self.to_variant(status)))
            except GLib.Error as error:
                log.debug("could not emit Changed: %s", error.message)
        if self.tray is not None:
            self.tray.update(status)

    # --- actions (D-Bus, tray, CLI) ----------------------------------------

    def _require_schedule(self):
        if self.schedule is None:
            raise RuntimeError(f"settings are broken: {self.config_error}")

    def set_level(self, level, until=policy.NEXT_PHASE):
        self._require_schedule()
        now = time.time()
        until = policy.resolve_until(until, now, self.schedule)
        for output in self.outputs.values():
            output.manual_hold = None
        paused = self.state.pause is not None
        policy.set_level(self.state, level, until)
        if paused:
            log.info("paused level changed to %d%%", clamp_level(level))
        else:
            log.info("manual level %d%% %s", clamp_level(level), self._describe_until(until))
        self._save_state()
        self.tick()

    def adjust(self, delta):
        self._require_schedule()
        self.set_level(clamp_level(self._shown_level() + delta), policy.NEXT_PHASE)

    def step(self, direction):
        """One step of Brighter/Dimmer, the same as a brightness key."""
        self._require_schedule()
        self.set_level(policy.step_level(self._shown_level(), direction, self.cfg.step),
                       policy.NEXT_PHASE)

    def _shown_level(self):
        """The level on screen: a level set with the brightness keys wins."""
        for output in self.outputs.values():
            if output.manual_hold is not None:
                return output.manual_hold.level
        return self.decision.level if self.decision else 50

    def pause(self, until=policy.FOREVER):
        self._require_schedule()
        now = time.time()
        until = policy.resolve_until(until, now, self.schedule)
        current = self.decision.level if self.decision else self.level_now()
        policy.pause(self.state, current, until)
        log.info("paused at %d%% %s", self.state.pause.level, self._describe_until(until))
        self._save_state()
        self.tick()

    def resume(self):
        policy.resume(self.state)
        for output in self.outputs.values():
            output.manual_hold = None
        log.info("resumed the schedule")
        self._save_state()
        self.tick()

    def preview(self, level):
        self._require_schedule()
        policy.preview(self.state, level, time.time())
        if self._preview_source:
            GLib.source_remove(self._preview_source)
        self._preview_source = GLib.timeout_add(
            int(policy.PREVIEW_SECONDS * 1000) + 200, self._preview_lapsed)
        self.tick()

    def _preview_lapsed(self):
        self._preview_source = None
        self.tick()
        return False

    def end_preview(self):
        if self.state.preview is not None:
            self.state.preview = None
            self.tick()

    def open_settings(self, *args):
        spawn_detached([launcher_path("luxtide-settings"), *args])

    def set_autostart(self, enabled):
        from . import autostart
        try:
            autostart.set_enabled(enabled)
            log.info("start at login %s", "on" if enabled else "off")
        except OSError as error:
            log.warning("could not change start at login: %s", error)
        if self.tray is not None:
            self.tray.update(self._status or self.status())

    def hide_tray(self):
        self.cfg.tray_enabled = False
        try:
            config_module.save(self.cfg)
        except OSError as error:
            log.warning("could not save settings: %s", error)
        self._sync_tray()

    def _describe_until(self, until):
        if until is None:
            return "until resumed"
        return time.strftime("until %H:%M", time.localtime(until))

    # --- D-Bus -------------------------------------------------------------

    def _on_method(self, connection, sender, path, interface, method, params, invocation):
        try:
            args = params.unpack()
            if method == "GetStatus":
                invocation.return_value(GLib.Variant.new_tuple(self.to_variant(self.status())))
                return
            if method == "GetForecast":
                fc = self.forecast
                points = list(zip(fc.times, fc.cloud)) if fc else []
                invocation.return_value(GLib.Variant(
                    "(da(xd))", (fc.fetched_at if fc else 0.0, points)))
                return
            if method == "SetLevel":
                self.set_level(args[0], args[1])
            elif method == "Adjust":
                self.adjust(args[0])
            elif method == "Step":
                self.step(args[0])
            elif method == "Pause":
                self.pause(args[0])
            elif method == "Resume":
                self.resume()
            elif method == "Preview":
                self.preview(args[0])
            elif method == "EndPreview":
                self.end_preview()
            elif method == "Reload":
                self.reload_config()
            elif method == "RefreshWeather":
                if not self.cfg.weather_enabled:
                    raise RuntimeError("weather is disabled in the settings")
                self.weather_check(force=True)
            invocation.return_value(None)
        except ValueError as error:
            invocation.return_dbus_error(f"{ERROR}.InvalidArgs", str(error))
        except Exception as error:
            log.exception("D-Bus %s failed", method)
            invocation.return_dbus_error(f"{ERROR}.Failed", str(error))

    def _already_running(self):
        """Checked before touching any display: a second copy, say from a
        login entry next to an enabled unit, must leave quietly."""
        reply = self.session.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (BUS_NAME,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, 5000, None)
        return reply.unpack()[0]

    def _export(self):
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        self.session.register_object(OBJECT_PATH, node.interfaces[0], self._on_method,
                                     None, None)
        Gio.bus_own_name_on_connection(self.session, BUS_NAME,
                                       Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                                       None, self._name_lost)

    def _name_lost(self, connection, name):
        log.error("another instance already owns %s", name)
        self.exit_code = 1
        self.loop.quit()

    def _watch_sleep(self):
        try:
            self.system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as error:
            log.warning("no system bus, suspend will go unnoticed: %s", error.message)
            return
        self.system.signal_subscribe(
            "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
            "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self._on_sleep_signal)

    def _on_sleep_signal(self, connection, sender, path, interface, name, params):
        if params.unpack()[0]:
            log.info("system suspending")
            for output in self.outputs.values():
                output.on_sleep()
        else:
            log.info("system resumed")
            GLib.timeout_add_seconds(2, self._after_resume)

    def _after_resume(self):
        self.rescan()
        self.poll()
        self._arm_poll()
        self.weather_check()
        self.follow_soon(FOLLOW_NETWORK_DELAY, "resume")
        self.tick()
        return False

    def _watch_config(self):
        directory = paths.config_file().parent
        directory.mkdir(parents=True, exist_ok=True)
        self._config_monitor = Gio.File.new_for_path(str(directory)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        self._config_monitor.connect("changed", self._on_config_event)

    def _on_config_event(self, monitor, file, other, event):
        names = {file.get_basename(), other.get_basename() if other else None}
        if paths.config_file().name not in names:
            return
        if self._reload_source:
            GLib.source_remove(self._reload_source)
        self._reload_source = GLib.timeout_add(400, self._reload_later)

    def _reload_later(self):
        self._reload_source = None
        self.reload_config()
        return False

    def _network_changed(self, monitor, available):
        if available:
            # A laptop starts before its Wi-Fi is up; the first download fails
            # and would otherwise wait 15 minutes.
            self.weather_retry_at = 0.0
            self.weather_check()
            self.follow_soon(FOLLOW_NETWORK_DELAY, "network changed")

    def _sync_tray(self):
        wanted = self.tray_allowed and self.cfg.tray_enabled
        if wanted and self.tray is None and self.session is not None:
            from .tray import Tray
            self.tray = Tray(self.session, self)
            self.tray.update(self._status or self.status())
        elif not wanted and self.tray is not None:
            self.tray.close()
            self.tray = None

    # --- life cycle --------------------------------------------------------

    def quit(self, *_):
        log.info("stopping")
        self.loop.quit()
        return False

    def run(self):
        log.info("luxtide %s%s", __version__, " (dry run)" if self.dry_run else "")
        if self.schedule is not None:
            lat, lon, source = self.schedule.latitude, self.schedule.longitude, \
                self.schedule.location_source
            log.info("location %.4f, %.4f (%s), time zone %s", lat, lon,
                     "from settings" if source == "config" else "from time zone",
                     self.cfg.zone_name())
        self.session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if self._already_running():
            log.info("already running, nothing to do")
            sdnotify.ready()                # a clean exit, not a failed start
            return 0
        self._export()
        sdnotify.ready()                    # on a laptop the desktop waited for this
        self.tick()
        self.rescan(initial=True)
        self.tick()
        self._watch_sleep()
        self._watch_desktop()
        self._check_ambient()
        self._watch_config()
        self._sync_tray()
        GLib.timeout_add_seconds(TICK_SECONDS, self.tick)
        GLib.timeout_add_seconds(RESCAN_SECONDS, self.rescan)
        GLib.timeout_add_seconds(WEATHER_CHECK_SECONDS, self.weather_check)
        monitor = Gio.NetworkMonitor.get_default()
        monitor.connect("network-changed", self._network_changed)
        self.follow_soon(FOLLOW_START_DELAY, "start")
        self.weather_check()
        for number in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number, self.quit)
        self.loop.run()
        for output in self.outputs.values():
            output.close()
        if self.tray is not None:
            self.tray.close()
        return self.exit_code
