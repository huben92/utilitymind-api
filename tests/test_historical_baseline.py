from datetime import datetime, timedelta, timezone
import unittest

from historical_baseline import FACTORY_TIMEZONE, expected_series, profile_bounds, slot_key
from operations_app import record_historical_deviation
from seed_historical_data import normal_rate, work_fraction


class RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, parameters):
        self.statements.append((sql, parameters))


class HistoricalBaselineTests(unittest.TestCase):
    def test_slot_uses_factory_local_time(self):
        self.assertEqual(slot_key(datetime(2026, 10, 18, 0, 10, tzinfo=timezone.utc)), (6, 28))

    def test_series_requires_every_meter_to_avoid_understating_baseline(self):
        end = datetime(2026, 10, 18, 0, 10, tzinfo=timezone.utc)
        key = slot_key(end)
        profiles = {(meter, *key): {"expected": rate, "lower_bound": rate * .8,
                                   "upper_bound": rate * 1.2}
                    for meter, rate in (("a", 10), ("b", 20))}
        self.assertEqual(expected_series(profiles, ["a", "b"], end, 1)[0]["value"], 30)
        self.assertEqual(expected_series(profiles, ["a", "missing"], end, 1), [])

    def test_empirical_band_has_margin_above_normal_variation(self):
        expected, lower, upper = profile_bounds(100, 90, 110, "kW")
        self.assertEqual((expected, lower, upper), (100, 81, 121))

    def test_synthetic_profiles_keep_furnace_standby_and_monday_generator_test(self):
        sunday = datetime(2026, 10, 18, 7, tzinfo=timezone.utc)
        monday = datetime(2026, 10, 19, 1, tzinfo=timezone.utc)
        furnace = {"id": "melt-gas", "machine_id": 3, "machine_code": "MELT-01",
                   "stage": "Melting", "type": "Natural Gas", "nominal": 80, "max_value": 150}
        generator = {**furnace, "id": "gen-diesel", "machine_code": "GEN-01",
                     "stage": "Emergency power", "type": "Diesel", "nominal": 30}
        self.assertGreater(normal_rate(furnace, sunday), 0)
        self.assertEqual(normal_rate(generator, sunday), 0)
        self.assertGreater(work_fraction(monday.astimezone(FACTORY_TIMEZONE), "Emergency power"), 0)

    def test_high_and_low_deviations_use_distinct_episodes(self):
        sensor = {"id": "m1", "name": "Power", "machine_name": "CNC 01", "location_name": "Hall",
                  "type": "Electricity", "unit": "kW", "enabled": True}
        profile = {"expected": 50, "lower_bound": 40, "upper_bound": 60}
        now = datetime.now(timezone.utc)
        conn = RecordingConnection()
        record_historical_deviation(conn, sensor, 70, now, now, profile)
        self.assertEqual(conn.statements[0][1][7], "historical_high")
        self.assertEqual(conn.statements[0][1][8], 60)
        conn = RecordingConnection()
        record_historical_deviation(conn, sensor, 35, now, now, profile)
        self.assertEqual(conn.statements[1][1][7], "historical_low")
        self.assertIn("LEAST", conn.statements[1][0])
        conn = RecordingConnection()
        record_historical_deviation(conn, sensor, 50, now, now, profile)
        self.assertTrue(all("resolved_at" in sql for sql, _ in conn.statements))


if __name__ == "__main__":
    unittest.main()
