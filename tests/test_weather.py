import io
import json
import tempfile
import time
import unittest
import pathlib
import urllib.parse

import helpers  # noqa: F401
from ddc_sun_dimmer import weather

SAMPLE = {"hourly": {"time": [1000, 4600, 8200, 11800],
                     "cloud_cover": [0, 100, None, 40]}}


class Forecasts(unittest.TestCase):
    def test_parse_and_interpolate(self):
        fc = weather.parse_response(SAMPLE, 50.0614, 19.9366, 5000)
        self.assertEqual(fc.times, [1000, 4600, 11800])      # the null hour is dropped
        self.assertEqual((fc.latitude, fc.longitude), (50.06, 19.94))
        self.assertAlmostEqual(fc.cloud_at(1000), 0)
        self.assertAlmostEqual(fc.cloud_at(2800), 50)
        self.assertAlmostEqual(fc.cloud_at(8200), 70)
        self.assertIsNone(fc.cloud_at(999))
        self.assertIsNone(fc.cloud_at(11801))

    def test_usable(self):
        fc = weather.parse_response(SAMPLE, 50.0614, 19.9366, 5000)
        self.assertTrue(fc.usable(6000, 50.061, 19.941, max_age_hours=1))
        self.assertFalse(fc.usable(6000, 52.0, 21.0, max_age_hours=1))      # elsewhere
        self.assertFalse(fc.usable(5000 + 3601, 50.06, 19.94, max_age_hours=1))  # stale
        self.assertFalse(fc.usable(20000, 50.06, 19.94, max_age_hours=12))  # past the end

    def test_bad_responses(self):
        for data in ({}, {"hourly": {"time": [1], "cloud_cover": [5]}},
                     {"hourly": {"time": "x", "cloud_cover": None}}):
            with self.assertRaises(weather.WeatherError):
                weather.parse_response(data, 0, 0, 0)

    def test_url_rounds_the_location(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(
            weather.build_url(50.061412, 19.936634)).query)
        self.assertEqual(query["latitude"], ["50.06"])
        self.assertEqual(query["longitude"], ["19.94"])
        self.assertEqual(query["hourly"], ["cloud_cover"])

    def test_fetch_with_fake_network(self):
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["agent"] = request.get_header("User-agent")
            return Response(json.dumps(SAMPLE).encode())

        fc = weather.fetch(50.06, 19.94, opener=opener)
        self.assertEqual(len(fc.times), 3)
        self.assertIn("api.open-meteo.com", seen["url"])
        self.assertTrue(seen["agent"].startswith("ddc-sun-dimmer/"))

        def failing(request, timeout):
            raise OSError("network down")
        with self.assertRaises(weather.WeatherError):
            weather.fetch(50.06, 19.94, opener=failing)

    def test_cache_round_trip(self):
        fc = weather.parse_response(SAMPLE, 50.06, 19.94, time.time())
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "forecast.json"
            weather.save_cache(fc, path)
            self.assertEqual(weather.load_cache(path), fc)
            path.write_text("{broken")
            self.assertIsNone(weather.load_cache(path))


if __name__ == "__main__":
    unittest.main()
