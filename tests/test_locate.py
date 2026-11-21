"""Finding the location: distances, place search parsing, which fixes to adopt."""

import io
import json
import unittest
import urllib.parse

import helpers  # noqa: F401
from luxtide import locate

GDANSK = (54.352, 18.6466)
KRAKOW = (50.0614, 19.9366)


class Distances(unittest.TestCase):
    def test_known_distance(self):
        # Gdańsk - Kraków is about 485 km in a straight line.
        self.assertAlmostEqual(locate.distance_m(*GDANSK, *KRAKOW) / 1000, 485, delta=5)

    def test_zero_and_symmetric(self):
        self.assertEqual(locate.distance_m(*GDANSK, *GDANSK), 0)
        self.assertAlmostEqual(locate.distance_m(*GDANSK, *KRAKOW),
                               locate.distance_m(*KRAKOW, *GDANSK))

    def test_accuracy_text(self):
        self.assertEqual(locate.accuracy_text(37), "±37 m")
        self.assertEqual(locate.accuracy_text(240), "±240 m")
        self.assertEqual(locate.accuracy_text(2500), "±2.5 km")
        self.assertEqual(locate.accuracy_text(25000), "±25 km")
        self.assertEqual(locate.accuracy_text(0), "accuracy unknown")


class Following(unittest.TestCase):
    def fix(self, lat, lon, accuracy):
        return locate.Fix(lat, lon, accuracy)

    def test_rough_fixes_are_never_taken(self):
        far = self.fix(*KRAKOW, 25000)            # an IP address estimate
        self.assertFalse(locate.should_adopt(GDANSK, far))
        self.assertFalse(locate.should_adopt(None, far))
        self.assertFalse(locate.should_adopt(GDANSK, self.fix(*KRAKOW, 0)))

    def test_small_moves_are_ignored(self):
        near = self.fix(GDANSK[0] + 0.005, GDANSK[1], 40)      # about 550 m
        self.assertFalse(locate.should_adopt(GDANSK, near))
        moved = self.fix(GDANSK[0] + 0.02, GDANSK[1], 40)      # about 2.2 km
        self.assertTrue(locate.should_adopt(GDANSK, moved))

    def test_first_good_fix_is_taken(self):
        self.assertTrue(locate.should_adopt(None, self.fix(*GDANSK, 80)))


class PlaceSearch(unittest.TestCase):
    RESPONSE = {"results": [
        {"name": "Kraków", "latitude": 50.06143, "longitude": 19.93658,
         "admin1": "Lesser Poland", "country": "Poland", "population": 755050},
        {"name": "Krakow", "latitude": 44.0, "longitude": -89.0, "country": "United States"},
        {"name": "broken"},
    ]}

    def test_parse(self):
        places = locate.parse_places(self.RESPONSE)
        self.assertEqual(len(places), 2)
        self.assertEqual(places[0].label(), "Kraków, Lesser Poland, Poland")
        self.assertEqual(places[0].short(), "Kraków, Lesser Poland")
        self.assertEqual(places[1].label(), "Krakow, United States")
        self.assertEqual(locate.parse_places({}), [])

    def test_url(self):
        url = locate.build_search_url("  Kraków ", language="pl")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["name"], ["Kraków"])
        self.assertEqual(query["language"], ["pl"])

    def test_language_from_the_environment(self):
        self.assertEqual(locate.search_language({"LANG": "pl_PL.UTF-8"}), "pl")
        self.assertEqual(locate.search_language({"LANGUAGE": "de:en", "LANG": "pl_PL"}), "de")
        self.assertEqual(locate.search_language({"LANG": "C.UTF-8"}), "en")
        self.assertEqual(locate.search_language({}), "en")

    def test_search_uses_the_response(self):
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            return io.BytesIO(json.dumps(self.RESPONSE).encode())
        places = locate.search_places("Kraków", language="en", opener=opener)
        self.assertEqual(places[0].name, "Kraków")
        self.assertIn("geocoding-api.open-meteo.com", seen["url"])
        self.assertEqual(locate.search_places("K", opener=opener), [])    # too short

    def test_errors(self):
        def refused(request, timeout):
            return io.BytesIO(b'{"error": true, "reason": "bad"}')

        def offline(request, timeout):
            raise OSError("no network")
        with self.assertRaises(locate.SearchError):
            locate.search_places("Kraków", opener=refused)
        with self.assertRaises(locate.SearchError):
            locate.search_places("Kraków", opener=offline)


class GeoClueFix(unittest.TestCase):
    def test_from_properties(self):
        fix = locate.fix_from_properties({"Latitude": 54.1, "Longitude": 18.7,
                                          "Accuracy": 42.0, "Description": "WiFi"})
        self.assertEqual((fix.latitude, fix.longitude, fix.accuracy), (54.1, 18.7, 42.0))


if __name__ == "__main__":
    unittest.main()
