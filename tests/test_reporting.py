"""Checks for coverage-aware utility reporting and PDF delivery."""

from datetime import datetime, timezone
import unittest

from report_pdf import make_report_pdf
from reporting import build_report, period_bounds


class ReportingTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 17, 8, tzinfo=timezone.utc)
        self.meters = [{
            "id": "meter-1", "name": "Electricity 01", "type": "Electricity",
            "unit": "kW", "baseline": 10, "machine_id": 1,
            "machine_name": "Charging Area", "machine_code": "CHG",
            "location_name": "Main Hall",
        }]

    def test_coverage_limits_integrated_total_and_baseline(self):
        period = period_bounds("daily", "2026-10-16", self.now)
        first = datetime(2026, 10, 15, 17, tzinfo=timezone.utc)
        second = datetime(2026, 10, 15, 18, tzinfo=timezone.utc)
        rows = [
            ("meter-1", first, 8, 720, 720, first, first.replace(minute=59)),
            ("meter-1", second, 12, 360, 360, second, second.replace(minute=29)),
        ]
        report = build_report(period, self.meters, rows, 5)
        electricity = report["utilities"][0]
        self.assertEqual(electricity["observed_total"], 14)
        self.assertEqual(electricity["baseline_total"], 15)
        self.assertEqual(electricity["utilization_pct"], 93.3)
        self.assertEqual(electricity["coverage_pct"], 6.2)
        self.assertEqual(electricity["quantity_unit"], "kWh")
        self.assertEqual(electricity["trend"][0]["value"], 8)
        self.assertEqual(electricity["trend"][1]["value"], 6)
        self.assertIsNone(electricity["trend"][2]["value"])
        self.assertEqual(report["data_origin"], "Simulated")

    def test_no_readings_are_not_reported_as_zero_usage(self):
        period = period_bounds("daily", "2026-10-16", self.now)
        report = build_report(period, self.meters, [])
        self.assertIsNone(report["utilities"][0]["observed_total"])
        self.assertEqual(report["coverage_pct"], 0)
        self.assertEqual(report["data_origin"], "No readings")
        self.assertTrue(make_report_pdf(report).startswith(b"%PDF-"))

    def test_five_minute_synthetic_history_covers_the_hour(self):
        period = period_bounds("daily", "2026-10-16", self.now)
        hour = datetime(2026, 10, 15, 17, tzinfo=timezone.utc)
        rows = [("meter-1", hour, 12, 12, 12, hour,
                 hour.replace(minute=55), 12)]
        report = build_report(period, self.meters, rows, 5)
        meter = report["meter_quality"][0]
        self.assertEqual(meter["covered_seconds"], 3600)
        self.assertEqual(meter["observed_total"], 12)
        self.assertEqual(meter["coverage_pct"], 4.2)

    def test_current_month_is_partial_and_future_is_rejected(self):
        period = period_bounds("monthly", "2026-10", self.now)
        self.assertTrue(period["partial"])
        self.assertEqual(period["end_utc"], self.now)
        with self.assertRaises(ValueError):
            period_bounds("daily", "2026-10-18", self.now)


if __name__ == "__main__":
    unittest.main()
