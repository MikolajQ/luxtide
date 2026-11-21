"""The settings window, plus the small "pause for…" dialog used by the tray.

Changes apply as you make them: the window rewrites the settings file a
moment after the last edit and the daemon reloads it by itself.
"""

import copy
import datetime as dt
import os
import shutil
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import (SETTINGS_APP_ID, autostart, config as config_module, locate,  # noqa: E402
               paths, policy, solar, weather)
from ..client import Client, DaemonError, DaemonUnavailable  # noqa: E402
from ..config import METHODS, ConfigError, DisplaySettings  # noqa: E402
from ..prefs import load_ui_prefs, remember  # noqa: E402
from . import chart  # noqa: E402

SAVE_DELAY_MS = 600
PREVIEW_DELAY_MS = 200
PREVIEW_RENEW_S = 10
ACCENT = (0.21, 0.52, 0.89, 1.0)

CSS = """
.section-title { font-weight: bold; margin-top: 10px; }
.panel { border: 1px solid alpha(currentColor, 0.14); border-radius: 12px; padding: 14px 16px; }
.big-level { font-size: 2.4em; font-weight: 300; }
.dim { opacity: 0.65; }
.small { font-size: 0.9em; }
.error-text { color: #c01c28; }
.banner { background: alpha(#e5a50a, 0.20); border-radius: 10px; padding: 10px 14px; }
.warning-text { color: #c64600; }
"""

CHART_HINT = ("Drag the round handles to change levels and the square ones to widen or "
              "narrow the ramps.")

MODES = ("time", "elevation")


def clock(timestamp):
    return time.strftime("%H:%M", time.localtime(timestamp)) if timestamp else "—"


def duration_text(minutes):
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" + (f" {rest} min" if rest else "")


def label(text="", css=(), xalign=0.0, wrap=False, max_chars=-1):
    widget = Gtk.Label(label=text, xalign=xalign, wrap=wrap, max_width_chars=max_chars)
    for name in css:
        widget.add_css_class(name)
    return widget


def start_daemon():
    """Start the daemon the way a login does (systemd unit, or in place)."""
    try:
        process = Gio.Subprocess.new([autostart.executable(), "autostart"],
                                     Gio.SubprocessFlags.STDOUT_SILENCE
                                     | Gio.SubprocessFlags.STDERR_SILENCE)
        process.wait_async(None, None, None)
        return True
    except GLib.Error:
        return False


class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Luxtide")
        self.set_default_size(820, 960)
        self.status = {}
        self.forecast_points = []
        self.forecast_stamp = None
        self.client = None
        self.daemon_up = False
        self.cfg, self.load_error = self._load()
        self.original = copy.deepcopy(self.cfg)
        self.date = dt.date.today()
        self.perceived = True
        self.model = None
        self.model_dirty = True
        self._loading = False
        self._save_source = None
        self._preview_source = None
        self._renew_source = None
        self._trying = None
        self._drag = None
        self._hover = None
        self._backed_up = False
        self._display_keys = None
        self._hint_source = None
        self._locate_window = None

        self._build()
        self._fill()
        self._load_forecast_cache()
        self._connect_daemon()
        self._watch_config()
        self.connect("close-request", self._on_close)
        GLib.timeout_add_seconds(30, self._refresh_now)

    # --- configuration ---------------------------------------------------------

    def _load(self):
        try:
            return config_module.load(), None
        except ConfigError as error:
            cfg = config_module.Config()
            cfg.source = paths.config_file()
            return cfg, str(error)

    def changed(self):
        """Something in the form changed: redraw, validate, save soon."""
        if self._loading:
            return
        self._update_sensitivity()
        try:
            self.cfg.validate()
            self.error_label.set_text("")
            valid = True
        except ConfigError as error:
            self.error_label.set_text(str(error))
            valid = False
        self.model_dirty = True
        self.area.queue_draw()
        if valid:
            if self._save_source:
                GLib.source_remove(self._save_source)
            self._save_source = GLib.timeout_add(SAVE_DELAY_MS, self._save)

    def _save(self):
        self._save_source = None
        path = self.cfg.source or paths.config_file()
        if self.load_error and not self._backed_up and path.exists():
            backup = path.with_name(f"{path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, backup)
            self._backed_up = True
            self.load_error = None
            self.banner_text.set_text(f"The unreadable file was kept as {backup.name}.")
        try:
            config_module.save(self.cfg, path)
            self.path_label.set_text(f"Saved to {short_path(path)}")
        except OSError as error:
            self.error_label.set_text(f"Could not save: {error}")
        return False

    def _watch_config(self):
        """Pick up changes made elsewhere: by hand, or by the daemon when it
        follows this computer's location. Without this, the next save from
        this window would put the old values back."""
        path = self.cfg.source or paths.config_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._config_monitor = Gio.File.new_for_path(str(path.parent)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
        except (GLib.Error, OSError):
            return
        self._config_monitor.connect("changed", self._config_event)
        self._config_reload_source = None

    def _config_event(self, monitor, file, other, event):
        path = self.cfg.source or paths.config_file()
        names = {file.get_basename(), other.get_basename() if other else None}
        if path.name not in names:
            return
        if self._config_reload_source:
            GLib.source_remove(self._config_reload_source)
        self._config_reload_source = GLib.timeout_add(400, self._config_reload)

    def _config_reload(self):
        self._config_reload_source = None
        if self._save_source or self.load_error:
            return False                     # our own pending save wins
        path = self.cfg.source or paths.config_file()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        if text == config_module.render(self.cfg):
            return False                     # what we wrote ourselves
        try:
            fresh = config_module.parse(text, path)
            fresh.validate()
        except ConfigError:
            return False                     # half-edited by hand; wait for the next save
        fresh.source = path
        self.cfg = fresh
        self._fill()
        self.model_dirty = True
        self.area.queue_draw()
        self.path_label.set_text(f"Reloaded {short_path(path)}: changed outside this window")
        return False

    # --- layout ----------------------------------------------------------------

    def _build(self):
        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        self.resume_button = Gtk.Button(icon_name="media-playback-start-symbolic",
                                        tooltip_text="Back to the schedule")
        self.resume_button.connect("clicked", lambda *_: self._call("resume"))
        self.pause_button = Gtk.MenuButton(icon_name="media-playback-pause-symbolic",
                                           tooltip_text="Pause automatic brightness")
        self.pause_button.set_popover(self._build_pause_popover())
        header.pack_end(self.pause_button)
        header.pack_end(self.resume_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                          margin_top=16, margin_bottom=20, margin_start=20, margin_end=20)
        self.content = content
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=content)
        self.set_child(scroller)

        content.append(self._build_status())
        content.append(self._build_chart())
        content.append(self._section("Levels", self._build_levels()))
        content.append(self._section("Transition", self._build_transition()))
        content.append(self._section("Weather", self._build_weather()))
        content.append(self._section("Location", self._build_location()))
        content.append(self._expander("Displays", self._build_displays()))
        content.append(self._expander("DDC/CI monitors", self._build_ddc()))
        content.append(self._expander("Laptop backlight", self._build_backlight()))
        content.append(self._expander("General", self._build_general()))
        content.append(self._build_footer())

    def _section(self, title, child):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(label(title, ("section-title",)))
        child.add_css_class("panel")
        box.append(child)
        return box

    def _expander(self, title, child):
        expander = Gtk.Expander(label=title, margin_top=8)
        child.add_css_class("panel")
        child.set_margin_top(6)
        expander.set_child(child)
        return expander

    def _grid(self):
        return Gtk.Grid(column_spacing=14, row_spacing=10)

    def _row(self, grid, row, text, widget, hint=None):
        grid.attach(label(text), 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        if hint:
            # A bounded natural width leaves the spare room to the controls.
            grid.attach(label(hint, ("dim", "small"), wrap=True, max_chars=34), 2, row, 1, 1)

    def _switch(self, callback):
        switch = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        switch.connect("notify::active", lambda w, _p: callback(w.get_active()))
        return switch

    def _spin(self, low, high, step, digits, callback):
        adjustment = Gtk.Adjustment(value=low, lower=low, upper=high, step_increment=step,
                                    page_increment=step * 10)
        spin = Gtk.SpinButton(adjustment=adjustment, digits=digits, numeric=True,
                              halign=Gtk.Align.START)
        adjustment.connect("value-changed", lambda a: callback(a.get_value()))
        return spin

    # --- status ----------------------------------------------------------------

    def _build_status(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.banner = Gtk.Box(spacing=12)
        self.banner.add_css_class("banner")
        self.banner_text = label("", wrap=True)
        self.banner_text.set_hexpand(True)
        self.banner_button = Gtk.Button(label="Start", valign=Gtk.Align.CENTER)
        self.banner_button.connect("clicked", self._start_daemon)
        self.banner.append(self.banner_text)
        self.banner.append(self.banner_button)
        box.append(self.banner)

        self.location_banner = Gtk.Box(spacing=12, visible=False)
        self.location_banner.add_css_class("banner")
        text = label("Where are you? With the reference city of your time zone the sun "
                     "times are roughly right, but the weather cannot be used.", wrap=True)
        text.set_hexpand(True)
        button = Gtk.Button(label="Find my location…", valign=Gtk.Align.CENTER)
        button.connect("clicked", self._open_locate)
        self.location_banner.append(text)
        self.location_banner.append(button)
        box.append(self.location_banner)

        self.ambient_banner = Gtk.Box(spacing=12, visible=False)
        self.ambient_banner.add_css_class("banner")
        text = label("GNOME's automatic brightness is off. Its light sensor would set the "
                     "built-in panel too, and the two would fight. To let the sensor decide "
                     "instead, turn off “Control the built-in panel” under Laptop backlight "
                     "below, then turn automatic brightness back on in GNOME's power "
                     "settings.", wrap=True)
        text.set_hexpand(True)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                          valign=Gtk.Align.CENTER)
        button = Gtk.Button(label="Power settings…")
        button.connect("clicked", self._open_power_settings)
        buttons.append(button)
        button = Gtk.Button(label="Got it")
        button.connect("clicked", self._dismiss_ambient)
        buttons.append(button)
        self.ambient_banner.append(text)
        self.ambient_banner.append(buttons)
        box.append(self.ambient_banner)

        row = Gtk.Box(spacing=16)
        self.level_label = label("—", ("big-level",))
        row.append(self.level_label)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       valign=Gtk.Align.CENTER)
        self.mode_label = label("")
        self.detail_label = label("", ("dim",), wrap=True)
        text.append(self.mode_label)
        text.append(self.detail_label)
        row.append(text)
        box.append(row)
        return box

    def _open_power_settings(self, *_):
        if shutil.which("gnome-control-center"):
            Gio.Subprocess.new(["gnome-control-center", "power"], Gio.SubprocessFlags.NONE)

    def _dismiss_ambient(self, *_):
        remember("ambient_notice_seen", True)
        self.ambient_banner.set_visible(False)

    def _update_status(self):
        s = self.status
        self.ambient_banner.set_visible(
            bool(self.daemon_up and s.get("gnome_ambient_off"))
            and not load_ui_prefs().get("ambient_notice_seen"))
        if self.load_error:
            self.banner_text.set_text(f"The settings file could not be read: "
                                      f"{self.load_error}\nThe defaults are shown; the "
                                      f"first change replaces the file (a copy is kept).")
            self.banner_button.set_visible(False)
            self.banner.set_visible(True)
        elif not self.daemon_up:
            self.banner_text.set_text("The background service is not running, so nothing "
                                      "adjusts the brightness right now.")
            self.banner_button.set_visible(True)
            self.banner.set_visible(True)
        elif s.get("config_error"):
            self.banner_text.set_text(f"The service rejected the settings: {s['config_error']}")
            self.banner_button.set_visible(False)
            self.banner.set_visible(True)
        else:
            self.banner.set_visible(False)

        if not self.daemon_up or "level" not in s:
            self.level_label.set_text("—")
            self.mode_label.set_text("Service not running" if not self.daemon_up else "")
            self.detail_label.set_text("")
            self.resume_button.set_visible(False)
            self.pause_button.set_sensitive(self.daemon_up)
            return
        self.level_label.set_text(f"{s['level']} %")
        mode, until = s["mode"], s.get("until", 0)
        texts = {"auto": "Following the sun",
                 "override": f"Manual until {clock(until)}" if until else "Manual",
                 "paused": f"Paused until {clock(until)}" if until else "Paused until resumed",
                 "preview": "Trying a level"}
        self.mode_label.set_text(texts.get(mode, mode))
        details = []
        if mode == "auto" and s.get("next_change"):
            details.append(f"Next {s['next_level']} % at {clock(s['next_change'])}")
        elif mode != "auto":
            details.append(f"Schedule says {s.get('scheduled')} %")
        if s.get("sunrise"):
            details.append(f"sunrise {clock(s['sunrise'])}, sunset {clock(s['sunset'])}")
        if s.get("weather_enabled") and s.get("cloud", -1) >= 0:
            change = round((s.get("factor", 1) - 1) * 100)
            details.append(f"cloud {s['cloud']:.0f} %" + (f" ({change:+d} %)" if change else ""))
        self.detail_label.set_text(" · ".join(details))
        self.resume_button.set_visible(mode in ("override", "paused"))
        self.pause_button.set_sensitive(True)

    # --- pause popover -----------------------------------------------------------

    def _build_pause_popover(self):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8,
                      margin_bottom=8, margin_start=8, margin_end=8)
        box.append(label("Pause automatic brightness", ("section-title",)))
        self.preset_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                      max_children_per_line=4, column_spacing=6,
                                      row_spacing=6)
        box.append(self.preset_box)
        custom = Gtk.Box(spacing=6)
        self.pause_minutes = Gtk.SpinButton.new_with_range(1, 1440, 5)
        self.pause_minutes.set_value(load_ui_prefs().get("last_pause_minutes", 45))
        custom.append(label("For"))
        custom.append(self.pause_minutes)
        custom.append(label("minutes"))
        go = Gtk.Button(label="Pause")
        go.add_css_class("suggested-action")
        go.connect("clicked", self._pause_custom)
        custom.append(go)
        box.append(custom)
        for text, until in (("Until the next phase", policy.NEXT_PHASE),
                            ("Until resumed", policy.FOREVER)):
            button = Gtk.Button(label=text)
            button.connect("clicked", lambda _b, u=until: self._pause(u))
            box.append(button)
        popover.set_child(box)
        popover.connect("show", lambda *_: self._fill_presets())
        self.pause_popover = popover
        return popover

    def _fill_presets(self):
        while child := self.preset_box.get_first_child():
            self.preset_box.remove(child)
        for minutes in self.cfg.tray_pause_presets:
            button = Gtk.Button(label=duration_text(minutes))
            button.connect("clicked", lambda _b, m=minutes: self._pause(int(time.time() + m * 60)))
            self.preset_box.append(button)

    def _pause_custom(self, *_):
        minutes = int(self.pause_minutes.get_value())
        remember("last_pause_minutes", minutes)
        self._pause(int(time.time() + minutes * 60))

    def _pause(self, until):
        self.pause_popover.popdown()
        self._call("pause", until)

    # --- chart -------------------------------------------------------------------

    def _build_chart(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.area = Gtk.DrawingArea(content_height=270, hexpand=True)
        self.area.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.area.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", self._leave)
        self.area.add_controller(motion)
        frame = Gtk.Frame(child=self.area)
        box.append(frame)

        bar = Gtk.Box(spacing=6)
        previous = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Previous day")
        previous.connect("clicked", lambda *_: self._shift_date(-1))
        following = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Next day")
        following.connect("clicked", lambda *_: self._shift_date(1))
        self.date_label = label("", xalign=0.5)
        self.date_label.set_size_request(130, -1)
        today = Gtk.Button(label="Today")
        today.connect("clicked", lambda *_: self._set_date(dt.date.today()))
        winter = Gtk.Button(label="Dec 21")
        winter.connect("clicked", lambda *_: self._set_date(dt.date(dt.date.today().year, 12, 21)))
        summer = Gtk.Button(label="Jun 21")
        summer.connect("clicked", lambda *_: self._set_date(dt.date(dt.date.today().year, 6, 21)))
        for widget in (previous, self.date_label, following, today, winter, summer):
            bar.append(widget)
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        d = config_module.Config()
        reset = Gtk.Button(
            label="Reset curve",
            tooltip_text=f"Back to the default curve: {d.day} / {d.twilight} / {d.night} %, "
                         f"ramps of ±{d.margin_minutes} min, geometric shape. Location, "
                         f"weather and displays stay as they are.")
        reset.connect("clicked", self._reset_curve)
        bar.append(reset)
        perceived = Gtk.CheckButton(label="Perceived scale", active=True,
                                    tooltip_text="Vertical axis in CIE lightness, "
                                                 "closer to what the eye sees")
        perceived.connect("toggled", self._toggle_perceived)
        bar.append(perceived)
        box.append(bar)
        self.chart_hint = label(CHART_HINT, ("dim", "small"), wrap=True)
        box.append(self.chart_hint)
        return box

    def _reset_curve(self, *_):
        if not config_module.reset_curve(self.cfg):
            self._flash_hint("The curve already has the default values.")
            return
        self._fill()
        self.changed()
        if self._trying:
            self._schedule_preview()
        self._flash_hint("Curve back to the defaults. “Undo my changes” at the bottom "
                         "restores the curve you had when this window opened.")

    def _flash_hint(self, text, seconds=10):
        """Show a note under the chart for a while, then the usual hint again."""
        self.chart_hint.set_text(text)
        if self._hint_source:
            GLib.source_remove(self._hint_source)

        def restore():
            self._hint_source = None
            self.chart_hint.set_text(CHART_HINT)
            return False
        self._hint_source = GLib.timeout_add_seconds(seconds, restore)

    def _shift_date(self, days):
        self._set_date(self.date + dt.timedelta(days=days))

    def _set_date(self, date):
        self.date = date
        self.date_label.set_text(date.strftime("%a %d %b %Y"))
        self.model_dirty = True
        self.area.queue_draw()

    def _toggle_perceived(self, button):
        self.perceived = button.get_active()
        self.model_dirty = True
        self.area.queue_draw()

    def _refresh_now(self):
        self.model_dirty = True
        self.area.queue_draw()
        return True

    def _ensure_model(self):
        if self.model_dirty or self.model is None:
            try:
                self.model = chart.build_model(self.cfg, self.date, self.forecast_points,
                                               time.time(), self.perceived)
            except (ConfigError, ValueError) as error:
                self.model = None
                self.chart_error = str(error)
            self.model_dirty = False
        return self.model

    def _draw(self, area, cr, width, height):
        model = self._ensure_model()
        color = area.get_color()
        fg = (color.red, color.green, color.blue, color.alpha)
        if model is None:
            cr.set_source_rgba(*fg)
            cr.move_to(20, height / 2)
            cr.show_text(getattr(self, "chart_error", "No location"))
            return
        model.hover = self._hover
        model.active = self._drag[0].name if self._drag else None
        chart.draw(cr, width, height, model, fg=fg, accent=ACCENT)

    def _drag_begin(self, gesture, x, y):
        model = self._ensure_model()
        if model is None:
            return
        handle = chart.hit(model, self.area.get_width(), self.area.get_height(), x, y)
        if handle is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._drag = (handle, x, y)

    def _drag_update(self, gesture, dx, dy):
        if not self._drag or self.model is None:
            return
        handle, x0, y0 = self._drag
        geometry = chart.Geometry(self.area.get_width(), self.area.get_height(), self.model)
        if handle.axis == "y":
            level = geometry.level(y0 + dy)
            self.level_adjustments[handle.name].set_value(level)
        else:
            timestamp = geometry.timestamp(x0 + dx)
            rise, set_ = self.model.sunrise, self.model.sunset
            if rise is None or set_ is None:
                return
            noon = (rise + set_) / 2
            distance = (timestamp - rise) if handle.timestamp < noon else (set_ - timestamp)
            self.margin_spin.set_value(max(5, min(240, round(distance / 60))))

    def _drag_end(self, *_):
        self._drag = None
        self.area.queue_draw()

    def _motion(self, controller, x, y):
        model = self._ensure_model()
        if model is None:
            return
        geometry = chart.Geometry(self.area.get_width(), self.area.get_height(), model)
        timestamp = geometry.timestamp(x)
        curve = model.adjusted or model.base
        self._hover = (timestamp, chart.value_at(curve, timestamp))
        over = chart.hit(model, self.area.get_width(), self.area.get_height(), x, y)
        self.area.set_cursor_from_name(
            "grabbing" if self._drag else ("grab" if over else "default"))
        self.area.queue_draw()

    def _leave(self, *_):
        self._hover = None
        self.area.queue_draw()

    # --- levels ------------------------------------------------------------------

    def _build_levels(self):
        grid = self._grid()
        self.level_adjustments = {}
        self.try_buttons = {}
        names = (("day", "Day"), ("twilight", "At sunrise and sunset"), ("night", "Night"))
        for row, (name, text) in enumerate(names):
            adjustment = Gtk.Adjustment(value=50, lower=1, upper=100, step_increment=1,
                                        page_increment=10)
            scale = Gtk.Scale(adjustment=adjustment, hexpand=True, draw_value=False)
            scale.set_size_request(260, -1)
            spin = Gtk.SpinButton(adjustment=adjustment, numeric=True)
            try_button = Gtk.ToggleButton(label="Try",
                                          tooltip_text="Show this level on the displays "
                                                       "for a moment")
            try_button.connect("toggled", self._try_toggled, name)
            adjustment.connect("value-changed", self._level_changed, name)
            grid.attach(label(text), 0, row, 1, 1)
            grid.attach(scale, 1, row, 1, 1)
            grid.attach(spin, 2, row, 1, 1)
            grid.attach(try_button, 3, row, 1, 1)
            self.level_adjustments[name] = adjustment
            self.try_buttons[name] = try_button
        grid.attach(label("Percent of the display's range. 1 % is the floor: zero can switch "
                          "some backlights off entirely.", ("dim", "small"), wrap=True),
                    0, 3, 4, 1)
        self.step_spin = self._spin(1, 50, 1, 0, lambda v: self._set("step", int(v)))
        grid.attach(label("Step"), 0, 4, 1, 1)
        step_box = Gtk.Box(spacing=10)
        step_box.append(self.step_spin)
        step_box.append(label("% of the current level, at least 1 point: Brighter and Dimmer, "
                              "the scroll wheel over the tray icon and, on GNOME and Plasma, "
                              "the brightness keys", ("dim", "small"), wrap=True,
                              max_chars=60))
        grid.attach(step_box, 1, 4, 3, 1)
        return grid

    def _level_changed(self, adjustment, name):
        if self._loading:
            return
        setattr(self.cfg, name, int(adjustment.get_value()))
        self.changed()
        if self._trying == name:
            self._schedule_preview()

    def _try_toggled(self, button, name):
        if self._loading:
            return
        if button.get_active():
            for other, widget in self.try_buttons.items():
                if other != name and widget.get_active():
                    self._loading = True
                    widget.set_active(False)
                    self._loading = False
            self._trying = name
            if not self._preview_now():
                self._loading = True
                button.set_active(False)
                self._loading = False
                self._trying = None
                return
            if self._renew_source:
                GLib.source_remove(self._renew_source)
            self._renew_source = GLib.timeout_add_seconds(PREVIEW_RENEW_S, self._renew)
        elif self._trying == name:
            self._stop_trying()

    def _preview_now(self):
        if not self._trying:
            return False
        level = int(self.level_adjustments[self._trying].get_value())
        return self._call("preview", level)

    def _schedule_preview(self):
        if self._preview_source:
            GLib.source_remove(self._preview_source)
        self._preview_source = GLib.timeout_add(PREVIEW_DELAY_MS, self._preview_later)

    def _preview_later(self):
        self._preview_source = None
        self._preview_now()
        return False

    def _renew(self):
        if not self._trying:
            self._renew_source = None
            return False
        self._preview_now()
        return True

    def _stop_trying(self):
        self._trying = None
        if self._renew_source:
            GLib.source_remove(self._renew_source)
            self._renew_source = None
        if self.daemon_up:
            self._call("end_preview")

    # --- transition -------------------------------------------------------------

    def _build_transition(self):
        grid = self._grid()
        self.mode_dropdown = Gtk.DropDown.new_from_strings(
            ["Around sunrise and sunset", "By the height of the sun"])
        self.mode_dropdown.connect("notify::selected", self._mode_changed)
        self._row(grid, 0, "Ramps", self.mode_dropdown)

        self.smooth_check = Gtk.CheckButton(label="Smooth (otherwise jump between levels)")
        self.smooth_check.connect("toggled", lambda b: self._set("smooth", b.get_active()))
        grid.attach(self.smooth_check, 1, 1, 2, 1)

        self.margin_spin = self._spin(5, 240, 5, 0,
                                      lambda v: self._set("margin_minutes", int(v)))
        self.margin_label = label("Ramp length")
        grid.attach(self.margin_label, 0, 2, 1, 1)
        grid.attach(self.margin_spin, 1, 2, 1, 1)
        self.margin_hint = label("minutes before and after sunrise and sunset",
                                 ("dim", "small"))
        grid.attach(self.margin_hint, 2, 2, 1, 1)

        self.elevation_box = Gtk.Box(spacing=8)
        self.elevation_spins = {}
        for name, text in (("night_elevation", "night below"),
                           ("twilight_elevation", "twilight at"),
                           ("day_elevation", "day above")):
            spin = self._spin(-18, 30, 0.5, 1, lambda v, n=name: self._set(n, float(v)))
            self.elevation_spins[name] = spin
            self.elevation_box.append(label(text))
            self.elevation_box.append(spin)
            self.elevation_box.append(label("°"))
        self.elevation_label = label("Sun height")
        grid.attach(self.elevation_label, 0, 3, 1, 1)
        grid.attach(self.elevation_box, 1, 3, 2, 1)

        self.shape_adjustment = Gtk.Adjustment(value=100, lower=0, upper=100,
                                               step_increment=1, page_increment=10)
        shape = Gtk.Scale(adjustment=self.shape_adjustment, hexpand=True, draw_value=False)
        shape.add_mark(0, Gtk.PositionType.BOTTOM, "linear")
        shape.add_mark(67, Gtk.PositionType.BOTTOM, "≈ CIE L*")
        shape.add_mark(100, Gtk.PositionType.BOTTOM, "geometric")
        self.shape_adjustment.connect(
            "value-changed", lambda a: self._set("curve_shape", round(a.get_value() / 100, 2)))
        self._row(grid, 4, "Ramp shape", shape)
        grid.attach(label("Geometric looks even to the eye on displays whose units are "
                          "linear in light; linear lingers bright and then drops.",
                          ("dim", "small"), wrap=True), 1, 5, 2, 1)
        return grid

    def _mode_changed(self, dropdown, _param):
        self._set("mode", MODES[dropdown.get_selected()])

    def _set(self, name, value):
        if self._loading:
            return
        setattr(self.cfg, name, value)
        self.changed()

    def _update_sensitivity(self):
        elevation = self.cfg.mode == "elevation"
        for widget in (self.margin_spin, self.margin_label, self.margin_hint):
            widget.set_visible(not elevation)
        for widget in (self.elevation_box, self.elevation_label):
            widget.set_visible(elevation)
        manual = not self.use_zone_check.get_active()
        self.latitude_spin.set_sensitive(manual)
        self.longitude_spin.set_sensitive(manual)
        for widget in self.weather_widgets:
            widget.set_sensitive(self.cfg.weather_enabled)

    # --- weather -----------------------------------------------------------------

    def _build_weather(self):
        grid = self._grid()
        self.weather_switch = self._switch(lambda v: self._set("weather_enabled", v))
        self._row(grid, 0, "Dim on cloudy days", self.weather_switch)

        self.strength_adjustment = Gtk.Adjustment(value=20, lower=0, upper=90,
                                                  step_increment=1, page_increment=5)
        strength = Gtk.Scale(adjustment=self.strength_adjustment, hexpand=True,
                             draw_value=True, digits=0)
        strength.set_format_value_func(lambda _s, v: f"−{int(v)} %")
        strength.add_mark(20, Gtk.PositionType.BOTTOM, None)
        self.strength_adjustment.connect(
            "value-changed", lambda a: self._set("weather_strength", int(a.get_value())))
        self._row(grid, 1, "At full cloud cover", strength)
        strength_hint = label("Thinner cloud dims proportionally less. The effect fades "
                              "out towards night and never goes below the night level.",
                              ("dim", "small"), wrap=True)
        grid.attach(strength_hint, 1, 2, 2, 1)

        self.refresh_spin = self._spin(15, 1440, 15, 0,
                                       lambda v: self._set("weather_refresh_minutes", int(v)))
        self._row(grid, 3, "Refresh every", self.refresh_spin,
                  "minutes (the local model updates every 3 hours; hourly catches each "
                  "run promptly)")

        status_row = Gtk.Box(spacing=10)
        self.weather_label = label("", ("dim",), wrap=True)
        self.weather_label.set_hexpand(True)
        refresh = Gtk.Button(label="Refresh now")
        refresh.connect("clicked", lambda *_: self._call("refresh_weather"))
        status_row.append(self.weather_label)
        status_row.append(refresh)
        grid.attach(status_row, 0, 4, 3, 1)
        grid.attach(label("Forecast from open-meteo.com. It receives your location rounded "
                          "to about 1 km, and nothing else.", ("dim", "small"), wrap=True),
                    0, 5, 3, 1)
        self.weather_blocked_label = label(
            "Off until your location is set (Location, below): cloud cover in the "
            "reference city of your time zone says little about your sky.",
            ("warning-text",), wrap=True)
        grid.attach(self.weather_blocked_label, 0, 6, 3, 1)
        self.weather_widgets = [strength, self.refresh_spin, refresh, strength_hint]
        return grid

    def _update_weather_label(self):
        s = self.status
        blocked = self.cfg.weather_enabled and self.cfg.latitude is None
        self.weather_blocked_label.set_visible(blocked)
        if not self.cfg.weather_enabled:
            text = "Off."
        elif blocked:
            text = "Waiting for your location."
        elif not self.daemon_up:
            text = "The service fetches the forecast once it runs."
        elif s.get("cloud", -1) >= 0:
            change = round((s.get("factor", 1) - 1) * 100)
            text = (f"Cloud cover now {s['cloud']:.0f} %, brightness {change:+d} %. "
                    f"Updated {clock(s.get('weather_updated'))} ({weather.PROVIDER}).")
        elif s.get("weather_error"):
            text = f"No forecast: {s['weather_error']}"
        else:
            text = "No forecast yet."
        self.weather_label.set_text(text)

    def _load_forecast_cache(self):
        cached = weather.load_cache()
        if cached:
            self.forecast_points = list(zip(cached.times, cached.cloud))

    def _load_forecast(self):
        try:
            _, points = self.client.forecast()
            self.forecast_points = list(points)
        except (DaemonUnavailable, DaemonError):
            self._load_forecast_cache()
        self.model_dirty = True
        self.area.queue_draw()

    # --- location ----------------------------------------------------------------

    def _build_location(self):
        grid = self._grid()
        find_row = Gtk.Box(spacing=12)
        find = Gtk.Button(label="Find my location…")
        find.add_css_class("suggested-action")
        find.connect("clicked", self._open_locate)
        self.location_source_label = label("", ("dim",), wrap=True)
        self.location_source_label.set_hexpand(True)
        find_row.append(find)
        find_row.append(self.location_source_label)
        grid.attach(find_row, 0, 0, 3, 1)
        self.location_warning = label("", ("warning-text",), wrap=True)
        grid.attach(self.location_warning, 0, 1, 3, 1)

        self.use_zone_check = Gtk.CheckButton(label="Use the reference city of my time zone")
        self.use_zone_check.connect("toggled", self._zone_toggled)
        grid.attach(self.use_zone_check, 0, 2, 3, 1)
        self.latitude_spin = self._spin(-90, 90, 0.01, 4,
                                        lambda v: self._set_location("latitude", v))
        self.longitude_spin = self._spin(-180, 180, 0.01, 4,
                                         lambda v: self._set_location("longitude", v))
        self._row(grid, 3, "Latitude", self.latitude_spin, "degrees, north positive")
        self._row(grid, 4, "Longitude", self.longitude_spin, "degrees, east positive")
        self.timezone_entry = Gtk.Entry(hexpand=False, width_chars=24)
        self.timezone_entry.connect("changed", self._timezone_changed)
        self._row(grid, 5, "Time zone", self.timezone_entry, "empty = follow the system")
        self.follow_location_switch = self._switch(self._follow_changed)
        self._row(grid, 6, "Keep it up to date", self.follow_location_switch,
                  "for laptops: asks the location service at start-up and when the "
                  "network changes, and takes only fixes within 3 km")
        self.location_note = label("", ("dim", "small"), wrap=True)
        grid.attach(self.location_note, 0, 7, 3, 1)
        return grid

    def _open_locate(self, *_):
        from .locate_window import LocateWindow
        if self._locate_window is not None:
            self._locate_window.present()
            return
        try:
            lat, lon, source = self.cfg.location()
        except ConfigError:
            lat, lon, source = 52.0, 19.0, "timezone"
        window = LocateWindow(self, (lat, lon, source == "config"), self._location_chosen)
        window.connect("close-request", lambda *_: setattr(self, "_locate_window", None))
        self._locate_window = window
        window.present()

    def _location_chosen(self, latitude, longitude, method, place, accuracy):
        self.cfg.set_location(latitude, longitude, method, place=place, accuracy=accuracy)
        self._loading = True
        self.use_zone_check.set_active(False)
        self.latitude_spin.set_value(self.cfg.latitude)
        self.longitude_spin.set_value(self.cfg.longitude)
        self._loading = False
        self._fill_location_note()
        self.changed()

    def _follow_changed(self, value):
        if self._loading:
            return
        self.cfg.location_follow = value
        self._fill_location_note()
        self.changed()

    def _zone_toggled(self, check):
        if self._loading:
            return
        if check.get_active():
            self.cfg.latitude = self.cfg.longitude = None
            self.cfg.location_method = self.cfg.location_place = ""
            self.cfg.location_accuracy = 0
        else:
            self.cfg.set_location(self.latitude_spin.get_value(),
                                  self.longitude_spin.get_value(), "manual")
        self._fill_location_note()
        self.changed()

    def _set_location(self, name, value):
        if self._loading or self.use_zone_check.get_active():
            return
        self.cfg.set_location(self.latitude_spin.get_value(), self.longitude_spin.get_value(),
                              "manual")
        self._fill_location_note()
        self.changed()

    def _timezone_changed(self, entry):
        if self._loading:
            return
        text = entry.get_text().strip()
        if text:
            try:
                solar.load_timezone(text)
            except Exception:
                entry.add_css_class("error")
                return
        entry.remove_css_class("error")
        self.cfg.timezone = text
        self._fill_location_note()
        self.changed()

    def _fill_location_note(self):
        c = self.cfg
        zone = c.zone_name()
        coords = solar.zone_coordinates(zone)
        self.location_banner.set_visible(c.latitude is None)
        if c.latitude is None:
            self.location_source_label.set_text("")
            city = zone.split("/")[-1].replace("_", " ")
            if coords:
                self.location_warning.set_text(
                    f"Using {city}, the reference city of {zone}. Sunrise and sunset may "
                    f"be off by up to half an hour, and the weather correction stays off: "
                    f"cloud cover there says little about your sky.")
            else:
                self.location_warning.set_text(f"{zone} has no reference city: find your "
                                               f"location or enter coordinates.")
            self.location_warning.set_visible(True)
        else:
            source = locate.describe_source(c.location_method, c.location_place,
                                            c.location_accuracy, c.location_updated)
            self.location_source_label.set_text(source)
            rough = c.location_method == "geoclue" and c.location_accuracy and \
                not locate.good_for_weather(c.location_accuracy)
            self.location_warning.set_text(
                "This position is only a rough estimate: search for your town or click on "
                "the map for a better one." if rough else "")
            self.location_warning.set_visible(bool(rough))
        error = self.status.get("location_error") if c.location_follow else ""
        note = ("Coordinates stay on this computer, except for the rounded weather request"
                + (" and the location service's Wi-Fi lookup." if c.location_follow else "."))
        if error:
            note = f"Last automatic update failed: {error}\n" + note
        self.location_note.set_text(note)
        if hasattr(self, "weather_blocked_label"):
            self._update_weather_label()

    # --- displays ----------------------------------------------------------------

    def _build_displays(self):
        self.displays_grid = self._grid()
        self.displays_grid.attach(label("Start the service to list displays.", ("dim",)),
                                  0, 0, 1, 1)
        return self.displays_grid

    def _update_displays(self):
        outputs = self.status.get("outputs", []) if self.daemon_up else []
        keys = tuple((o["key"], o["label"], o["method"], o.get("note", "")) for o in outputs)
        if keys == self._display_keys:
            return
        self._display_keys = keys
        grid = self.displays_grid
        while child := grid.get_first_child():
            grid.remove(child)
        if not outputs:
            grid.attach(label("No controllable display found." if self.daemon_up
                              else "Start the service to list displays.", ("dim",)), 0, 0, 1, 1)
            return
        grid.attach(label("Enabled", ("dim", "small")), 1, 0, 1, 1)
        grid.attach(label("Offset", ("dim", "small")), 2, 0, 1, 1)
        for row, output in enumerate(outputs, start=1):
            key = output["key"]
            settings = self.cfg.display(key)
            name = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            name.append(label(output["label"]))
            name.append(label(f"{output['method']} · settings name {key}", ("dim", "small")))
            if output.get("note"):
                name.append(label(output["note"], ("small",), wrap=True, max_chars=50))
            switch = Gtk.Switch(active=settings.enabled, valign=Gtk.Align.CENTER)
            switch.connect("notify::active",
                           lambda w, _p, k=key: self._set_display(k, enabled=w.get_active()))
            offset = Gtk.SpinButton.new_with_range(-50, 50, 1)
            offset.set_value(settings.offset)
            offset.set_tooltip_text("Points added to the common level, to match displays")
            offset.connect("value-changed",
                           lambda w, k=key: self._set_display(k, offset=int(w.get_value())))
            grid.attach(name, 0, row, 1, 1)
            grid.attach(switch, 1, row, 1, 1)
            grid.attach(offset, 2, row, 1, 1)

    def _set_display(self, key, **changes):
        current = copy.deepcopy(self.cfg.display(key))
        for name, value in changes.items():
            setattr(current, name, value)
        if current.is_default():
            self.cfg.displays.pop(key, None)
        else:
            self.cfg.displays[key] = current
        self.changed()

    # --- DDC -----------------------------------------------------------------------

    def _build_ddc(self):
        grid = self._grid()
        self.ddc_widgets = {}

        def ddc_set(name, value):
            if self._loading:
                return
            setattr(self.cfg.ddc, name, value)
            self.changed()

        self.ddc_widgets["enabled"] = self._switch(lambda v: ddc_set("enabled", v))
        self._row(grid, 0, "Control monitors over DDC/CI", self.ddc_widgets["enabled"])
        rows = (("repeats", "Repeat each write", 1, 10, 1, 0,
                 "times; against monitors that silently drop frames"),
                ("retries", "Retries on error", 1, 20, 1, 0, "per write"),
                ("sleep_multiplier", "Bus delay multiplier", 0.1, 10, 0.1, 1,
                 "ddcutil --sleep-multiplier"),
                ("settle", "Pause between repeats", 0, 5, 0.1, 1, "seconds"),
                ("refresh_minutes", "Re-write while dimmed every", 0, 1440, 1, 0,
                 "minutes, 0 = never"),
                ("restore_level", "Monitor falls back to", 1, 100, 1, 0,
                 "% by itself (no periodic re-writes at this level)"))
        for row, (name, text, low, high, step, digits, hint) in enumerate(rows, start=1):
            convert = int if digits == 0 else float
            spin = self._spin(low, high, step, digits,
                              lambda v, n=name, c=convert: ddc_set(n, c(v)))
            self.ddc_widgets[name] = spin
            self._row(grid, row, text, spin, hint)
        self.ddc_widgets["wake_burst"] = self._switch(lambda v: ddc_set("wake_burst", v))
        self._row(grid, 7, "Re-assert after wake-up", self.ddc_widgets["wake_burst"],
                  "for monitors that restore their own brightness seconds after waking")
        self.ddc_widgets["skip_ddc_checks"] = self._switch(
            lambda v: ddc_set("skip_ddc_checks", v))
        self._row(grid, 8, "Skip ddcutil's checks", self.ddc_widgets["skip_ddc_checks"],
                  "faster writes, quieter journal")
        mccs = Gtk.Entry(width_chars=6)
        mccs.connect("changed", lambda e: ddc_set("mccs", e.get_text().strip()))
        self.ddc_widgets["mccs"] = mccs
        self._row(grid, 9, "MCCS version", mccs, "e.g. 2.0, empty = detect")
        return grid

    # --- backlight ---------------------------------------------------------------

    def _build_backlight(self):
        grid = self._grid()
        self.backlight_switch = self._switch(lambda v: self._set("backlight_enabled", v))
        self._row(grid, 0, "Control the built-in panel", self.backlight_switch)
        self.method_dropdown = Gtk.DropDown.new_from_strings(
            ["Automatic", "GNOME Shell", "GNOME (Mutter)", "KDE Plasma", "logind", "sysfs"])
        self.method_dropdown.connect(
            "notify::selected",
            lambda d, _p: self._set("backlight_method", METHODS[d.get_selected()]))
        self._row(grid, 1, "Through", self.method_dropdown,
                  "the desktop's own interface keeps its brightness slider in sync")
        self.follow_switch = self._switch(lambda v: self._set("backlight_follow_manual", v))
        self._row(grid, 2, "Respect brightness keys", self.follow_switch,
                  "a change made with the keys holds until the next phase")
        return grid

    # --- general -----------------------------------------------------------------

    def _build_general(self):
        grid = self._grid()
        self.tray_switch = self._switch(lambda v: self._set("tray_enabled", v))
        self._row(grid, 0, "Tray icon", self.tray_switch,
                  "on GNOME this needs the AppIndicator extension")
        self.show_level_switch = self._switch(lambda v: self._set("tray_show_level", v))
        self._row(grid, 1, "Percentage next to the icon", self.show_level_switch,
                  "GNOME only")
        self.presets_entry = Gtk.Entry(width_chars=20)
        self.presets_entry.connect("changed", self._presets_changed)
        self._row(grid, 2, "Pause durations", self.presets_entry,
                  "minutes, comma separated, offered in the tray menu")
        self.autostart_switch = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        self.autostart_switch.connect("notify::active", self._autostart_changed)
        self._row(grid, 3, "Start at login", self.autostart_switch,
                  "also in the tray menu and in the desktop's startup settings")
        return grid

    def _presets_changed(self, entry):
        if self._loading:
            return
        try:
            values = sorted({int(v) for v in entry.get_text().replace(";", ",").split(",")
                             if v.strip()})
            if not values or any(not 1 <= v <= 1440 for v in values):
                raise ValueError
        except ValueError:
            entry.add_css_class("error")
            return
        entry.remove_css_class("error")
        self.cfg.tray_pause_presets = values[:8]
        self.changed()

    def _autostart_changed(self, switch, _param):
        if self._loading:
            return
        try:
            autostart.set_enabled(switch.get_active())
        except OSError as error:
            self.error_label.set_text(f"Could not change start at login: {error}")

    # --- footer --------------------------------------------------------------------

    def _build_footer(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=12)
        self.error_label = label("", ("error-text",), wrap=True)
        box.append(self.error_label)
        row = Gtk.Box(spacing=8)
        self.path_label = label("", ("dim", "small"), wrap=True)
        self.path_label.set_hexpand(True)
        undo = Gtk.Button(label="Undo my changes",
                          tooltip_text="Restore the settings as they were when this "
                                       "window opened")
        undo.connect("clicked", self._undo)
        open_file = Gtk.Button(label="Open file")
        open_file.connect("clicked", self._open_file)
        row.append(self.path_label)
        row.append(open_file)
        row.append(undo)
        box.append(row)
        return box

    def _undo(self, *_):
        self.cfg = copy.deepcopy(self.original)
        self._fill()
        self._loading = False
        self.changed()

    def _open_file(self, *_):
        path = self.cfg.source or paths.config_file()
        if not path.exists():
            config_module.save(self.cfg, path)
        Gtk.FileLauncher.new(Gio.File.new_for_path(str(path))).launch(self, None, None, None)

    # --- filling the form --------------------------------------------------------

    def _fill(self):
        self._loading = True
        c = self.cfg
        for name in ("day", "twilight", "night"):
            self.level_adjustments[name].set_value(getattr(c, name))
        self.step_spin.set_value(c.step)
        self.mode_dropdown.set_selected(MODES.index(c.mode))
        self.smooth_check.set_active(c.smooth)
        self.margin_spin.set_value(c.margin_minutes)
        for name, spin in self.elevation_spins.items():
            spin.set_value(getattr(c, name))
        self.shape_adjustment.set_value(round(c.curve_shape * 100))
        self.weather_switch.set_active(c.weather_enabled)
        self.strength_adjustment.set_value(c.weather_strength)
        self.refresh_spin.set_value(c.weather_refresh_minutes)
        self.use_zone_check.set_active(c.latitude is None)
        try:
            lat, lon, _ = c.location()
        except ConfigError:
            lat, lon = 0.0, 0.0
        self.latitude_spin.set_value(lat)
        self.longitude_spin.set_value(lon)
        self.timezone_entry.set_text(c.timezone)
        self.timezone_entry.set_placeholder_text(
            f"system ({solar.system_timezone_name() or 'UTC'})")
        self.follow_location_switch.set_active(c.location_follow)
        self._fill_location_note()
        d = c.ddc
        self.ddc_widgets["enabled"].set_active(d.enabled)
        for name in ("repeats", "retries", "sleep_multiplier", "settle", "refresh_minutes",
                     "restore_level"):
            self.ddc_widgets[name].set_value(getattr(d, name))
        self.ddc_widgets["wake_burst"].set_active(d.wake_burst)
        self.ddc_widgets["skip_ddc_checks"].set_active(d.skip_ddc_checks)
        self.ddc_widgets["mccs"].set_text(d.mccs)
        self.backlight_switch.set_active(c.backlight_enabled)
        self.method_dropdown.set_selected(METHODS.index(c.backlight_method))
        self.follow_switch.set_active(c.backlight_follow_manual)
        self.tray_switch.set_active(c.tray_enabled)
        self.show_level_switch.set_active(c.tray_show_level)
        self.presets_entry.set_text(", ".join(str(p) for p in c.tray_pause_presets))
        self.autostart_switch.set_active(autostart.is_enabled())
        self._set_date(self.date)
        self.path_label.set_text(short_path(c.source or paths.config_file()))
        self._display_keys = None
        self._loading = False
        self._update_sensitivity()
        self._update_status()
        self._update_weather_label()

    # --- daemon ------------------------------------------------------------------

    def _connect_daemon(self):
        try:
            self.client = Client()
        except DaemonUnavailable:
            self.client = None
            self._update_status()
            return
        self.client.subscribe(self._on_status)
        self.client.watch(self._daemon_appeared, self._daemon_vanished)

    def _daemon_appeared(self):
        self.daemon_up = True
        try:
            self._on_status(self.client.status())
        except (DaemonUnavailable, DaemonError):
            self.daemon_up = False
        self._load_forecast()

    def _daemon_vanished(self):
        self.daemon_up = False
        self.status = {}
        self._update_status()
        self._update_displays()
        self._update_weather_label()

    def _on_status(self, status):
        self.daemon_up = True
        stamp = status.get("weather_updated")
        self.status = status
        self._update_status()
        self._update_displays()
        self._fill_location_note()
        if stamp != self.forecast_stamp:
            self.forecast_stamp = stamp
            self._load_forecast()

    def _call(self, method, *args):
        if self.client is None:
            self.error_label.set_text("No session bus.")
            return False
        try:
            getattr(self.client, method)(*args)
            return True
        except DaemonUnavailable:
            self.error_label.set_text("The service is not running: start it first.")
        except DaemonError as error:
            self.error_label.set_text(str(error))
        return False

    def _start_daemon(self, *_):
        if not start_daemon():
            self.error_label.set_text("Could not start the service; is it installed?")

    def _on_close(self, *_):
        if self._trying:
            self._stop_trying()
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save()
        return False


class PauseWindow(Gtk.ApplicationWindow):
    """"Pause for…" from the tray: any length, or until a time of day."""

    def __init__(self, app):
        super().__init__(application=app, title="Pause brightness", resizable=False)
        self.client = None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18,
                      margin_bottom=18, margin_start=18, margin_end=18)
        box.append(label("Pause automatic brightness", ("section-title",)))
        prefs = load_ui_prefs()
        self.for_radio = Gtk.CheckButton(label="For", active=True)
        self.minutes = Gtk.SpinButton.new_with_range(1, 1440, 5)
        self.minutes.set_value(prefs.get("last_pause_minutes", 45))
        row = Gtk.Box(spacing=8)
        row.append(self.for_radio)
        row.append(self.minutes)
        row.append(label("minutes"))
        box.append(row)

        self.until_radio = Gtk.CheckButton(label="Until", group=self.for_radio)
        later = time.localtime(time.time() + 3600)
        self.hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.hour.set_value(later.tm_hour)
        self.minute = Gtk.SpinButton.new_with_range(0, 59, 5)
        self.minute.set_value(later.tm_min - later.tm_min % 5)
        for spin in (self.hour, self.minute):
            spin.set_wrap(True)
            spin.set_orientation(Gtk.Orientation.VERTICAL)
        row = Gtk.Box(spacing=8)
        row.append(self.until_radio)
        row.append(self.hour)
        row.append(label(":"))
        row.append(self.minute)
        box.append(row)
        self.minutes.connect("value-changed", lambda *_: self.for_radio.set_active(True))
        self.hour.connect("value-changed", lambda *_: self.until_radio.set_active(True))
        self.minute.connect("value-changed", lambda *_: self.until_radio.set_active(True))

        self.message = label("", ("error-text",), wrap=True)
        box.append(self.message)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        ok = Gtk.Button(label="Pause")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", self._pause)
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)
        self.set_child(box)
        self.set_default_widget(ok)

    def _pause(self, *_):
        if self.for_radio.get_active():
            minutes = int(self.minutes.get_value())
            until = int(time.time() + minutes * 60)
            remember("last_pause_minutes", minutes)
        else:
            now = dt.datetime.now().astimezone()
            target = now.replace(hour=int(self.hour.get_value()),
                                 minute=int(self.minute.get_value()), second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            until = int(target.timestamp())
        try:
            Client().pause(until)
        except (DaemonUnavailable, DaemonError) as error:
            self.message.set_text(str(error))
            return
        self.close()


def short_path(path):
    """A path with the home directory written as ~, as people read it."""
    home = os.path.expanduser("~")
    text = str(path)
    if home not in ("", "/") and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def save_screenshot(widget, path):
    """Render a widget to PNG (used to check and document the layout)."""
    width, height = widget.get_width(), widget.get_height()
    paintable = Gtk.WidgetPaintable.new(widget)
    snapshot = Gtk.Snapshot()
    # The widget alone has no window background; paint one matching the theme.
    color = widget.get_color()
    dark = (color.red + color.green + color.blue) / 3 > 0.5
    background = Gdk.RGBA()
    background.parse("#242424" if dark else "#fafafa")
    from gi.repository import Graphene
    snapshot.append_color(background, Graphene.Rect().init(0, 0, width, height))
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    renderer = widget.get_native().get_renderer()
    texture = renderer.render_texture(node, None)
    texture.save_to_png(path)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=SETTINGS_APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        if "--pause" in args:
            PauseWindow(self).present()
            return 0
        if self.window is None:
            self.window = SettingsWindow(self)
        self.window.present()
        if "--locate" in args:
            self.window._open_locate()
        if "--screenshot" in args:
            path = args[args.index("--screenshot") + 1]
            expand = "--expand" in args

            def shoot():
                if expand:
                    for expander in self._expanders(self.window.content):
                        expander.set_expanded(True)
                    GLib.timeout_add(600, finish)
                else:
                    finish()
                return False

            def finish():
                dialog = self.window._locate_window
                save_screenshot(dialog.get_child() if dialog else self.window.content, path)
                self.quit()
                return False
            # The map needs time for its tiles to arrive.
            GLib.timeout_add(6000 if "--locate" in args else 1500, shoot)
        return 0

    def _expanders(self, widget):
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Expander):
                yield child
            child = child.get_next_sibling()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    paths.migrate_legacy()
    return App().run([sys.argv[0], *argv])
