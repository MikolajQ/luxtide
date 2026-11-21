"""Finding where this computer is: the system location service and place search.

How precise is precise enough? The sun barely cares: one kilometre east or
west moves sunrise by about four seconds. The weather does: cloud cover is
forecast on a grid of a few kilometres, so the reference city of a time zone,
often hundreds of kilometres away, says little about the local sky. A
kilometre or two is plenty.

Two ways to get there:

- GeoClue, the desktop's location service. With Wi-Fi it locates within tens
  of metres from the networks in range (looked up in BeaconDB); a computer on
  a cable only gets an estimate from its IP address, often a city or more
  away, which is why every fix carries its accuracy. GNOME asks the user once
  and needs Location Services turned on.
- Place search through Open-Meteo's geocoding, the provider the forecast
  already comes from. It returns the centre of a town or village.
"""

import dataclasses
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request

from . import SETTINGS_APP_ID, __version__

log = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# What GNOME's permission dialog and the permission store know us by: the
# settings window's desktop entry, shared by the daemon so that one answer
# covers both.
DESKTOP_ID = SETTINGS_APP_ID

GEOCLUE = "org.freedesktop.GeoClue2"
MANAGER_PATH = "/org/freedesktop/GeoClue2/Manager"
ACCURACY_EXACT = 8

EARTH_RADIUS_M = 6_371_000


@dataclasses.dataclass
class Fix:
    latitude: float
    longitude: float
    accuracy: float            # metres, radius of the likely area
    description: str = ""


@dataclasses.dataclass
class Place:
    name: str
    latitude: float
    longitude: float
    region: str = ""           # province or state
    country: str = ""
    population: int = 0

    def label(self):
        return ", ".join(part for part in (self.name, self.region, self.country) if part)

    def short(self):
        return ", ".join(part for part in (self.name, self.region) if part)


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def accuracy_text(metres):
    if not metres or metres <= 0:
        return "accuracy unknown"
    if metres < 1000:
        return f"±{round(metres, -1) if metres >= 100 else round(metres):.0f} m"
    return f"±{metres / 1000:.0f} km" if metres >= 10_000 else f"±{metres / 1000:.1f} km"


def good_for_weather(metres):
    """Is a fix this precise useful for the cloud correction?"""
    return 0 < metres <= 3000


def should_adopt(current, fix, max_accuracy=3000, min_move=1000):
    """Following the computer: take `fix` over `current` (lat, lon) or None?

    Rough fixes (an IP address estimate) are never taken: they would drag a
    good position somewhere worse. Small moves are ignored, so the settings
    file is not rewritten for every Wi-Fi reconnect.
    """
    if fix.accuracy <= 0 or fix.accuracy > max_accuracy:
        return False
    if current is None:
        return True
    return distance_m(current[0], current[1], fix.latitude, fix.longitude) >= min_move


# --- place search ------------------------------------------------------------


def search_language(environ=None):
    """Two-letter language for place names, from the usual variables."""
    environ = os.environ if environ is None else environ
    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = environ.get(variable, "")
        code = value.split(":")[0].split(".")[0].split("_")[0].lower()
        if len(code) == 2:
            return code
    return "en"


def build_search_url(query, language=None, count=10):
    return GEOCODING_URL + "?" + urllib.parse.urlencode({
        "name": query.strip(), "count": count, "language": language or search_language(),
        "format": "json"})


def parse_places(data):
    places = []
    for item in (data or {}).get("results") or []:
        try:
            places.append(Place(
                name=str(item["name"]), latitude=float(item["latitude"]),
                longitude=float(item["longitude"]), region=str(item.get("admin1") or ""),
                country=str(item.get("country") or ""),
                population=int(item.get("population") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return places


class SearchError(Exception):
    pass


def search_places(query, language=None, timeout=15, opener=urllib.request.urlopen):
    """Towns and villages matching `query`. Blocking; call it off the main loop."""
    if len(query.strip()) < 2:
        return []
    request = urllib.request.Request(build_search_url(query, language),
                                     headers={"User-Agent": f"luxtide/{__version__}"})
    try:
        with opener(request, timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError) as error:
        raise SearchError(str(error)) from None
    if isinstance(data, dict) and data.get("error"):
        raise SearchError(str(data.get("reason", "request refused")))
    return parse_places(data)


# --- GeoClue -----------------------------------------------------------------


def explain_geoclue_error(error):
    """A GLib.Error from GeoClue, in words a user can act on."""
    from gi.repository import Gio
    remote = Gio.DBusError.get_remote_error(error) or ""
    if remote.endswith(("ServiceUnknown", "NameHasNoOwner")) or "was not provided" in \
            error.message:
        return ("The location service (GeoClue) is not installed. Search for your town "
                "or click on the map instead.")
    if remote.endswith("AccessDenied") or "denied" in error.message.lower() or \
            "disabled" in error.message.lower():
        return ("The location service refused. On GNOME turn on Settings → Privacy → "
                "Location Services; elsewhere search for your town or use the map.")
    return f"The location service failed: {error.message}"


def fix_from_properties(props):
    return Fix(latitude=float(props["Latitude"]), longitude=float(props["Longitude"]),
               accuracy=float(props.get("Accuracy", 0) or 0),
               description=str(props.get("Description", "") or ""))


class GeoClueLocator:
    """Asks GeoClue for this computer's position, on the GLib main loop.

    GeoClue usually answers first with a rough position and refines it as
    Wi-Fi results come in, so every fix goes to `on_fix` and the search
    stops once a fix is `good_enough` metres or `timeout` seconds have
    passed. `on_done(best_fix_or_None, error_text_or_None)` is called once.
    """

    def __init__(self, on_done, on_fix=None, timeout=30, good_enough=150,
                 desktop_id=DESKTOP_ID):
        self.on_done = on_done
        self.on_fix = on_fix
        self.timeout = timeout
        self.good_enough = good_enough
        self.desktop_id = desktop_id
        self.best = None
        self.bus = None
        self.client = None
        self._created = False
        self._subscription = None
        self._timer = None
        self._finished = False

    def start(self):
        from gi.repository import Gio, GLib
        self._timer = GLib.timeout_add_seconds(self.timeout, self._timed_out)
        Gio.bus_get(Gio.BusType.SYSTEM, None, self._got_bus)

    def cancel(self):
        self._finish(None, None, report=False)

    # The chain of asynchronous calls, one step each.

    def _call(self, path, interface, method, params, reply, then):
        from gi.repository import Gio, GLib
        self.bus.call(GEOCLUE, path, interface, method, params,
                      GLib.VariantType(reply) if reply else None,
                      Gio.DBusCallFlags.NONE, 10000, None, then)

    def _got_bus(self, _source, result):
        from gi.repository import Gio, GLib
        try:
            self.bus = Gio.bus_get_finish(result)
        except GLib.Error as error:
            return self._fail(error)
        self._call(MANAGER_PATH, f"{GEOCLUE}.Manager", "CreateClient", None, "(o)",
                   self._got_client)

    def _got_client(self, bus, result):
        from gi.repository import GLib
        try:
            self.client = bus.call_finish(result).unpack()[0]
            self._created = True
        except GLib.Error as error:
            if "UnknownMethod" in (error.message or "") and not self._created:
                # GeoClue before 2.5 only has one client per connection.
                return self._call(MANAGER_PATH, f"{GEOCLUE}.Manager", "GetClient", None,
                                  "(o)", self._got_client_legacy)
            return self._fail(error)
        self._configure()

    def _got_client_legacy(self, bus, result):
        from gi.repository import GLib
        try:
            self.client = bus.call_finish(result).unpack()[0]
        except GLib.Error as error:
            return self._fail(error)
        self._configure()

    def _configure(self):
        from gi.repository import GLib
        if self._finished:
            return
        self._set("DesktopId", GLib.Variant("s", self.desktop_id),
                  lambda: self._set("RequestedAccuracyLevel",
                                    GLib.Variant("u", ACCURACY_EXACT), self._subscribe))

    def _set(self, name, value, then):
        from gi.repository import GLib

        def done(bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error as error:
                return self._fail(error)
            then()
        self._call(self.client, "org.freedesktop.DBus.Properties", "Set",
                   GLib.Variant("(ssv)", (f"{GEOCLUE}.Client", name, value)), None, done)

    def _subscribe(self):
        from gi.repository import Gio
        if self._finished:
            return
        self._subscription = self.bus.signal_subscribe(
            GEOCLUE, f"{GEOCLUE}.Client", "LocationUpdated", self.client, None,
            Gio.DBusSignalFlags.NONE, self._on_location)
        self._call(self.client, f"{GEOCLUE}.Client", "Start", None, None, self._started)

    def _started(self, bus, result):
        from gi.repository import GLib
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            self._fail(error)

    def _on_location(self, bus, sender, path, interface, signal, params):
        from gi.repository import GLib
        new_path = params.unpack()[1]

        def got(bus, result):
            try:
                props = bus.call_finish(result).unpack()[0]
                fix = fix_from_properties(props)
            except (GLib.Error, KeyError, TypeError, ValueError) as error:
                log.debug("unreadable GeoClue location: %s", error)
                return
            if self._finished:
                return
            if self.best is None or fix.accuracy < self.best.accuracy:
                self.best = fix
                if self.on_fix:
                    self.on_fix(fix)
            if 0 < fix.accuracy <= self.good_enough:
                self._finish(self.best, None)
        self._call(new_path, "org.freedesktop.DBus.Properties", "GetAll",
                   GLib.Variant("(s)", (f"{GEOCLUE}.Location",)), "(a{sv})", got)

    def _timed_out(self):
        self._timer = None
        if self.best is not None:
            self._finish(self.best, None)
        else:
            self._finish(None, f"No position within {self.timeout} s. Without Wi-Fi "
                               "the location service may have nothing to go on.")
        return False

    def _fail(self, error):
        self._finish(None, explain_geoclue_error(error))

    def _finish(self, fix, error, report=True):
        from gi.repository import GLib
        if self._finished:
            return
        self._finished = True
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = None
        if self._subscription is not None:
            self.bus.signal_unsubscribe(self._subscription)
            self._subscription = None
        if self.client and self.bus:
            ignore = lambda bus, result: _quietly(bus.call_finish, result)
            self._call(self.client, f"{GEOCLUE}.Client", "Stop", None, None, ignore)
            if self._created:
                self._call(MANAGER_PATH, f"{GEOCLUE}.Manager", "DeleteClient",
                           GLib.Variant("(o)", (self.client,)), None, ignore)
        if report:
            self.on_done(fix, error)


def _quietly(function, *args):
    from gi.repository import GLib
    try:
        function(*args)
    except GLib.Error:
        pass


def wait_for_fix(timeout=30):
    """Blocking one-shot fix for the command line: (Fix or None, error or None)."""
    from gi.repository import GLib
    loop = GLib.MainLoop()
    outcome = {}

    def done(fix, error):
        outcome.update(fix=fix, error=error)
        loop.quit()
    GeoClueLocator(done, timeout=timeout).start()
    loop.run()
    return outcome.get("fix"), outcome.get("error")


METHOD_NAMES = {"manual": "entered by hand", "geoclue": "location service",
                "search": "place search", "map": "picked on the map"}


def describe_source(method, place="", accuracy=0, updated=""):
    """How the saved coordinates were found, e.g.
    "place search: Kraków, Małopolskie · ±2 km · 2026-11-21 11:30"."""
    if not method:
        return ""
    parts = [METHOD_NAMES.get(method, method) + (f": {place}" if place else "")]
    if accuracy:
        parts.append(accuracy_text(accuracy))
    if updated:
        parts.append(updated)
    return " · ".join(parts)


def now_text():
    return time.strftime("%Y-%m-%d %H:%M")
