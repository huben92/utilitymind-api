"""Baseline alerts use a strict threshold and do not duplicate open episodes."""

from datetime import datetime, timezone
import unittest

from operations_app import record_metering_alerts


class RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params):
        self.statements.append((sql, params))


class BaselineAlertTests(unittest.TestCase):
    def setUp(self):
        self.conn = RecordingConnection()
        self.meter = {"id": "meter-1", "name": "Electricity 01", "machine_name": "Machine A",
                      "location_name": "Hall A", "type": "Electricity", "unit": "kW",
                      "baseline": 50, "max_value": 100, "enabled": True}
        self.now = datetime(2026, 10, 17, tzinfo=timezone.utc)

    def record(self, value):
        before = len(self.conn.statements)
        record_metering_alerts(self.conn, self.meter, value, self.now, self.now)
        return self.conn.statements[before:]

    def test_exceedance_upserts_one_open_episode(self):
        (sql, params), (max_sql, max_params) = self.record(70)
        self.assertIn("ON CONFLICT (sensor_id,alert_type) WHERE resolved_at IS NULL DO UPDATE", sql)
        self.assertIn("exceedance_count=metering_alerts.exceedance_count+1", sql)
        self.assertEqual((params[0], params[6], params[7], params[8], params[9]),
                         ("meter-1", 50, "baseline", 50, 70))
        self.assertIn("UPDATE metering_alerts SET resolved_at", max_sql)
        self.assertEqual(max_params[-1], "maximum")

    def test_maximum_exceedance_opens_both_types(self):
        statements = self.record(120)
        self.assertEqual([params[7] for _, params in statements], ["baseline", "maximum"])
        self.assertEqual([params[8] for _, params in statements], [50, 100])

    def test_equal_or_lower_reading_resolves_episode(self):
        for value in (50, 40):
            statements = self.record(value)
            self.assertEqual(len(statements), 2)
            self.assertTrue(all("UPDATE metering_alerts SET resolved_at" in sql for sql, _ in statements))
            self.assertEqual([params[3] for _, params in statements], [value, value])

    def test_disabled_meter_does_not_generate_alert(self):
        self.meter["enabled"] = False
        record_metering_alerts(self.conn, self.meter, 120, self.now, self.now)
        self.assertFalse(self.conn.statements)


if __name__ == "__main__":
    unittest.main()
