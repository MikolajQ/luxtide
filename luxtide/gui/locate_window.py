"""The "Find my location" window: location service, place search and a map.

All three lead to one pin. The location service and the search place it, the
map shows it and a click moves it, and "Use this location" saves where it
stands, with a note of how it got there.
"""

import math
import threading

from gi.repository import GLib, Gtk

from .. import locate

try:
    import gi
    gi.require_version("Shumate", "1.0")
    from gi.repository import Shumate
except (ImportError, ValueError):
    Shumate = None

CLICK_SLOP = 6          # pixels a click may wander before it counts as a drag


def zoom_for_accuracy(metres):
    if 0 < metres <= 150:
        return 16
    if 0 < metres <= 1500:
        return 14
    if 0 < metres <= 10_000:
        return 11
    return 8


class Dot(Gtk.DrawingArea):
    """The pin: a dot centred exactly on the point, unlike a pin icon's tip."""

    SIZE = 22

    def __init__(self):
        super().__init__(content_width=self.SIZE, content_height=self.SIZE)
        self.set_draw_func(self._draw)
        self.set_can_target(False)

    def _draw(self, area, cr, width, height):
        radius = self.SIZE / 2 - 2
        cr.arc(width / 2, height / 2, radius, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.stroke()
        cr.arc(width / 2, height / 2, radius - 3, 0, 2 * math.pi)
        cr.set_source_rgba(0.21, 0.52, 0.89, 1.0)
        cr.fill()


class LocateWindow(Gtk.Window):
    def __init__(self, parent, start, on_chosen):
        """`start` is (latitude, longitude, known); `on_chosen(latitude,
        longitude, method, place, accuracy)` receives the saved choice."""
        super().__init__(title="Find my location", transient_for=parent, modal=True,
                         default_width=760, default_height=720)
        self.on_chosen = on_chosen
        self.pending = None
        self.locator = None
        self._search_serial = 0
        self._press = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16,
                      margin_bottom=16, margin_start=16, margin_end=16)
        self.set_child(box)

        row = Gtk.Box(spacing=10)
        self.locate_button = Gtk.Button(label="Use this computer's location")
        self.locate_button.connect("clicked", self._locate)
        self.spinner = Gtk.Spinner()
        self.locate_status = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        self.locate_status.add_css_class("dim")
        self.locate_status.set_text("Best on a laptop with Wi-Fi: usually within tens of "
                                    "metres.")
        row.append(self.locate_button)
        row.append(self.spinner)
        row.append(self.locate_status)
        box.append(row)

        self.search = Gtk.SearchEntry(placeholder_text="Or search for your town or village",
                                      search_delay=400)
        self.search.connect("search-changed", self._search_changed)
        self.search.connect("activate", self._search_activate)
        box.append(self.search)
        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results.connect("row-activated", self._result_chosen)
        self.results_scroller = Gtk.ScrolledWindow(
            child=self.results, hscrollbar_policy=Gtk.PolicyType.NEVER,
            propagate_natural_height=True, max_content_height=200, visible=False,
            has_frame=True)
        box.append(self.results_scroller)
        self.search_status = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.search_status.add_css_class("dim")
        box.append(self.search_status)

        if Shumate is not None:
            box.append(self._build_map(start))
            hint = "Click the map to put the dot exactly where you are."
        else:
            missing = Gtk.Label(label="Install libshumate to pick the place on a map.",
                                vexpand=True, wrap=True)
            missing.add_css_class("dim")
            box.append(missing)
            hint = ""
        if hint:
            hint_label = Gtk.Label(label=hint, xalign=0)
            hint_label.add_css_class("dim")
            hint_label.add_css_class("small")
            box.append(hint_label)

        bottom = Gtk.Box(spacing=10)
        self.pending_label = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.use_button = Gtk.Button(label="Use this location", sensitive=False)
        self.use_button.add_css_class("suggested-action")
        self.use_button.connect("clicked", self._use)
        bottom.append(self.pending_label)
        bottom.append(cancel)
        bottom.append(self.use_button)
        box.append(bottom)

        privacy = Gtk.Label(
            label="The location service looks up the Wi-Fi networks in range in BeaconDB; "
                  "the search goes to open-meteo.com; map tiles come from openstreetmap.org. "
                  "Only when you use them.", xalign=0, wrap=True)
        privacy.add_css_class("dim")
        privacy.add_css_class("small")
        box.append(privacy)

        self._show_pending()
        self.connect("close-request", self._closing)

    # --- map ---------------------------------------------------------------------

    def _build_map(self, start):
        latitude, longitude, known = start
        self.simple = Shumate.SimpleMap(vexpand=True, hexpand=True)
        registry = Shumate.MapSourceRegistry.new_with_defaults()
        self.simple.set_map_source(registry.get_by_id(Shumate.MAP_SOURCE_OSM_MAPNIK))
        self.map = self.simple.get_map()
        self.viewport = self.simple.get_viewport()
        layer = Shumate.MarkerLayer.new(self.viewport)
        self.marker = Shumate.Marker()
        self.marker.set_child(Dot())
        self.marker.set_visible(known)
        layer.add_marker(self.marker)
        self.map.add_layer(layer)
        self.marker.set_location(latitude, longitude)
        self.map.go_to_full_with_duration(latitude, longitude, 13 if known else 6, 0)
        click = Gtk.GestureClick()
        click.connect("pressed", self._map_pressed)
        click.connect("released", self._map_released)
        self.map.add_controller(click)
        frame = Gtk.Frame(child=self.simple, vexpand=True)
        return frame

    def _map_pressed(self, gesture, n_press, x, y):
        self._press = (x, y) if n_press == 1 else None

    def _map_released(self, gesture, n_press, x, y):
        if self._press is None or n_press != 1:
            return
        px, py = self._press
        self._press = None
        if math.hypot(x - px, y - py) > CLICK_SLOP:
            return
        latitude, longitude = self.viewport.widget_coords_to_location(self.map, x, y)
        self._set_pending(latitude, longitude, "map", "", 0, move_map=False)

    # --- the pending choice ----------------------------------------------------

    def _set_pending(self, latitude, longitude, method, place, accuracy, move_map=True,
                     zoom=None):
        self.pending = dict(latitude=latitude, longitude=longitude, method=method,
                            place=place, accuracy=accuracy)
        if Shumate is not None:
            self.marker.set_location(latitude, longitude)
            self.marker.set_visible(True)
            if move_map:
                self.map.go_to_full(latitude, longitude, zoom or zoom_for_accuracy(accuracy))
        self._show_pending()

    def _show_pending(self):
        if self.pending is None:
            self.pending_label.set_text("No place chosen yet.")
            self.use_button.set_sensitive(False)
            return
        p = self.pending
        source = locate.describe_source(p["method"], p["place"], p["accuracy"])
        self.pending_label.set_text(f"{p['latitude']:.4f}, {p['longitude']:.4f} · {source}")
        self.use_button.set_sensitive(True)

    def _use(self, *_):
        if self.pending is None:
            return
        p = self.pending
        self.on_chosen(p["latitude"], p["longitude"], p["method"], p["place"], p["accuracy"])
        self.close()

    def _closing(self, *_):
        if self.locator is not None:
            self.locator.cancel()
            self.locator = None
        return False

    # --- location service --------------------------------------------------------

    def _locate(self, *_):
        if self.locator is not None:
            return
        self.spinner.start()
        self.locate_button.set_sensitive(False)
        self.locate_status.set_text("Asking the location service…")
        self.locator = locate.GeoClueLocator(self._located, on_fix=self._fix, timeout=30,
                                             good_enough=100)
        self.locator.start()

    def _fix(self, fix):
        self._set_pending(fix.latitude, fix.longitude, "geoclue", "", fix.accuracy)
        self.locate_status.set_text(f"Found, {locate.accuracy_text(fix.accuracy)}; "
                                    "refining…")

    def _located(self, fix, error):
        self.locator = None
        self.spinner.stop()
        self.locate_button.set_sensitive(True)
        if fix is None:
            self.locate_status.set_text(error or "No position.")
            return
        self._set_pending(fix.latitude, fix.longitude, "geoclue", "", fix.accuracy)
        if locate.good_for_weather(fix.accuracy):
            self.locate_status.set_text(f"Found, {locate.accuracy_text(fix.accuracy)}.")
        else:
            self.locate_status.set_text(
                f"Only a rough estimate ({locate.accuracy_text(fix.accuracy)}), probably "
                "from the internet address. Search for your town or click on the map.")

    # --- place search ------------------------------------------------------------

    def _search_changed(self, entry):
        query = entry.get_text().strip()
        self._search_serial += 1
        serial = self._search_serial
        if len(query) < 2:
            self._show_results([], "")
            return
        self.search_status.set_text("Searching…")
        self.search_status.set_visible(True)

        def work():
            try:
                places, error = locate.search_places(query), ""
            except locate.SearchError as failure:
                places, error = [], f"Search failed: {failure}"
            GLib.idle_add(self._search_done, serial, places, error)
        threading.Thread(target=work, daemon=True, name="place-search").start()

    def _search_done(self, serial, places, error):
        if serial == self._search_serial:
            self._show_results(places, error or ("" if places else "Nothing found."))
        return False

    def _search_activate(self, entry):
        row = self.results.get_row_at_index(0)
        if row is not None:
            self._result_chosen(self.results, row)

    def _show_results(self, places, message):
        while (row := self.results.get_row_at_index(0)) is not None:
            self.results.remove(row)
        for place in places:
            row = Gtk.ListBoxRow()
            row.place = place
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6,
                           margin_bottom=6, margin_start=10, margin_end=10)
            title = Gtk.Label(label=place.name, xalign=0)
            where = Gtk.Label(label=", ".join(p for p in (place.region, place.country) if p),
                              xalign=0)
            where.add_css_class("dim")
            where.add_css_class("small")
            text.append(title)
            text.append(where)
            row.set_child(text)
            self.results.append(row)
        self.results_scroller.set_visible(bool(places))
        self.search_status.set_text(message)
        self.search_status.set_visible(bool(message))

    def _result_chosen(self, listbox, row):
        place = row.place
        self._set_pending(place.latitude, place.longitude, "search", place.short(), 0,
                          zoom=13)
        self.results_scroller.set_visible(False)
