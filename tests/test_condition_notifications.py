"""A missing stream must not create an off-machine or sustained-spike notice."""

from datetime import datetime, timedelta, timezone
import unittest

from operations_app import machine_condition_flags, update_condition_state


class StateConnection:
    def __init__(self):
        self.row = None

    def execute(self, sql, params):
        if sql.startswith("INSERT INTO metering_condition_state"):
            self.row = {"machine_id": params[1], "last_value": params[2], "last_seen_at": params[3],
                        "zero_since": params[4], "maximum_since": params[5]}
        return self

    def fetchone(self):
        return self.row


class ConditionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
        self.sensor = {"id": "meter-1", "machine_id": 1, "max_value": 100}

    def test_zero_and_maximum_require_continuous_minute(self):
        conn = StateConnection()
        state = update_condition_state(conn, self.sensor, 0, self.now)
        self.assertEqual(state["zero_since"], self.now)
        state = update_condition_state(conn, self.sensor, 0, self.now + timedelta(minutes=1))
        self.assertEqual(state["zero_since"], self.now + timedelta(minutes=1))
        for seconds in (65, 70, 75):
            state = update_condition_state(conn, self.sensor, 0, self.now + timedelta(seconds=seconds))
        self.assertEqual(state["zero_since"], self.now + timedelta(minutes=1))
        state = update_condition_state(conn, self.sensor, 120, self.now + timedelta(seconds=80))
        self.assertIsNone(state["zero_since"])
        self.assertEqual(state["maximum_since"], self.now + timedelta(seconds=80))
        state = update_condition_state(conn, self.sensor, 120, self.now + timedelta(seconds=85))
        self.assertEqual(state["maximum_since"], self.now + timedelta(seconds=80))
        state = update_condition_state(conn, self.sensor, 100, self.now + timedelta(seconds=90))
        self.assertIsNone(state["maximum_since"])

    def test_all_zero_requires_every_enabled_meter_fresh_for_a_minute(self):
        rows = [self.row("a", 0, 70), self.row("b", 0, 65)]
        self.assertEqual(machine_condition_flags(rows, self.now)[:3], (True, False, False))
        rows[1]["last_seen_at"] = self.now - timedelta(seconds=30)
        self.assertEqual(machine_condition_flags(rows, self.now)[:3], (False, False, False))

    def test_partial_zero_requires_other_positive_meter(self):
        rows = [self.row("a", 0, 65), self.row("b", 20, 0)]
        self.assertEqual(machine_condition_flags(rows, self.now)[:3], (False, True, True))
        rows[0]["zero_since"] = self.now - timedelta(seconds=30)
        self.assertEqual(machine_condition_flags(rows, self.now)[:3], (False, False, True))

    def row(self, name, value, zero_seconds):
        return {"name": name, "last_value": value, "last_seen_at": self.now,
                "zero_since": self.now - timedelta(seconds=zero_seconds) if value == 0 else None}


if __name__ == "__main__":
    unittest.main()
