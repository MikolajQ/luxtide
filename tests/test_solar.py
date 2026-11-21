import datetime as dt
import unittest
import zoneinfo

import helpers  # noqa: F401
from luxtide import solar

WARSAW = zoneinfo.ZoneInfo("Europe/Warsaw")


class SunTimes(unittest.TestCase):
    def test_sunrise_matches_elevation_model(self):
        # Sunrise and sunset are refined against the elevation model, so at
        # those moments the sun's centre must sit 0.833 degrees below the
        # horizon to within seconds.
        places = [(50.0614, 19.9366), (52.23, 21.01), (40.71, -74.0), (-33.87, 151.21),
                  (0.0, 0.0), (64.15, -21.94)]
        for lat, lon in places:
            for month in (1, 3, 6, 9, 12):
                date = dt.date(2026, month, 15)
                sun = solar.sun_day(date, lat, lon, dt.timezone.utc)
                for moment, rising in ((sun.rise, True), (sun.set, False)):
                    t = moment.timestamp()
                    crossing = self._crossing(t - 1800, t + 1800, lat, lon, rising)
                    self.assertLess(abs(crossing - t), 5, msg=f"{lat},{lon} {date} {moment}")

    @staticmethod
    def _crossing(low, high, lat, lon, rising):
        for _ in range(40):
            middle = (low + high) / 2
            above = solar.elevation(middle, lat, lon) > solar.HORIZON
            if above == rising:
                high = middle
            else:
                low = middle
        return (low + high) / 2

    def test_known_day_in_krakow(self):
        sun = solar.sun_day(dt.date(2026, 11, 7), 50.0614, 19.9366, WARSAW)
        self.assertEqual(sun.rise.strftime("%H:%M"), "06:39")
        self.assertEqual(sun.set.strftime("%H:%M"), "16:07")

    def test_polar_night_and_day(self):
        tromso = (69.65, 18.96)
        self.assertEqual(solar.sun_day(dt.date(2026, 12, 21), *tromso, WARSAW).polar, "night")
        self.assertEqual(solar.sun_day(dt.date(2026, 6, 21), *tromso, WARSAW).polar, "day")
        self.assertIsNone(solar.sun_day(dt.date(2026, 3, 21), *tromso, WARSAW).polar)

    def test_noon_elevation(self):
        # Equinox noon at the equator: the sun is nearly overhead.
        noon = dt.datetime(2026, 3, 20, 12, 7, tzinfo=dt.timezone.utc).timestamp()
        self.assertGreater(solar.elevation(noon, 0.0, 0.0), 88)


class Zones(unittest.TestCase):
    def test_iso6709(self):
        self.assertEqual(solar.parse_iso6709("+5215+02100"), (52.25, 21.0))
        lat, lon = solar.parse_iso6709("+404251-0740023")
        self.assertAlmostEqual(lat, 40.714167, places=5)
        self.assertAlmostEqual(lon, -74.006389, places=5)
        with self.assertRaises(ValueError):
            solar.parse_iso6709("north")

    def test_zone_coordinates(self):
        lat, lon = solar.zone_coordinates("Europe/Warsaw")
        self.assertAlmostEqual(lat, 52.25)
        self.assertAlmostEqual(lon, 21.0)
        self.assertIsNone(solar.zone_coordinates("Not/AZone"))
        self.assertIsNone(solar.zone_coordinates(""))


if __name__ == "__main__":
    unittest.main()
