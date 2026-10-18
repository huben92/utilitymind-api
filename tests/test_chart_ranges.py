from datetime import datetime, timedelta, timezone
import unittest

from chart_ranges import chart_window, historical_bucket_series
from historical_baseline import slot_key


NOW = datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc)  # Sunday, 13:00 WIB


class ChartRangeTests(unittest.TestCase):
    def test_factory_calendar_windows_and_resolution(self):
        today = chart_window({"range": "today"}, NOW)
        self.assertEqual(today["start"], datetime(2026, 10, 17, 17, tzinfo=timezone.utc))
        self.assertEqual(today["bucket_seconds"], 300)
        week = chart_window({"range": "week"}, NOW)
        self.assertEqual(week["start"], datetime(2026, 10, 11, 17, tzinfo=timezone.utc))
        self.assertEqual(week["bucket_seconds"], 1800)
        previous = chart_window({"range": "last_month"}, NOW)
        self.assertEqual(previous["start"], datetime(2026, 8, 31, 17, tzinfo=timezone.utc))
        self.assertEqual(previous["end"], datetime(2026, 9, 30, 17, tzinfo=timezone.utc))
        self.assertEqual(chart_window({"range": "3m"}, NOW)["bucket_seconds"], 21600)

    def test_custom_dates_include_final_factory_day_and_reject_invalid_windows(self):
        window = chart_window({"range": "custom", "start": "2026-09-01", "end": "2026-09-02"}, NOW)
        self.assertEqual(window["start"], datetime(2026, 8, 31, 17, tzinfo=timezone.utc))
        self.assertEqual(window["end"], datetime(2026, 9, 2, 17, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "End date"):
            chart_window({"range": "custom", "start": "2026-09-02", "end": "2026-09-01"}, NOW)
        with self.assertRaisesRegex(ValueError, "past time"):
            chart_window({"range": "custom", "start": "2026-10-19", "end": "2026-10-20"}, NOW)

    def test_historical_bucket_requires_all_meters_and_averages_rates(self):
        start = datetime(2026, 10, 18, 0, tzinfo=timezone.utc)
        window = {"start": start, "end": start + timedelta(minutes=30), "bucket_seconds": 1800}
        first, second = slot_key(start), slot_key(start + timedelta(minutes=15))
        profiles = {("a", *first): {"expected": 10}, ("b", *first): {"expected": 20},
                    ("a", *second): {"expected": 30}, ("b", *second): {"expected": 40}}
        self.assertEqual(historical_bucket_series(profiles, ["a", "b"], window)[0]["value"], 50)
        del profiles[("b", *second)]
        self.assertEqual(historical_bucket_series(profiles, ["a", "b"], window), [])


if __name__ == "__main__":
    unittest.main()
