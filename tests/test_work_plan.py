import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import patch

import operations_app
from work_plan import normalize_plan


def sample():
    return {"date": "2026-10-17", "start_time": "22:00", "pic": "Andi",
            "machines": [{"machine_id": 1, "planned_on": False}, {"machine_id": 2, "planned_on": True}]}


class WorkPlanTests(unittest.TestCase):
    def test_default_duration_and_overnight_window(self):
        plan = normalize_plan(sample())
        self.assertEqual(plan["duration_minutes"], 480)
        self.assertEqual(plan["start_at"], datetime(2026, 10, 17, 15, tzinfo=timezone.utc))
        self.assertEqual(plan["end_at"], datetime(2026, 10, 17, 23, tzinfo=timezone.utc))

    def test_rejects_missing_pic_duplicate_machine_and_invalid_state(self):
        item = sample(); item["pic"] = ""
        with self.assertRaisesRegex(ValueError, "PIC"):
            normalize_plan(item)
        item = sample(); item["machines"].append({"machine_id": 1, "planned_on": True})
        with self.assertRaisesRegex(ValueError, "only once"):
            normalize_plan(item)
        item = sample(); item["machines"][0]["planned_on"] = "off"
        with self.assertRaisesRegex(ValueError, "ON/OFF"):
            normalize_plan(item)

    def test_rejects_invalid_duration_and_empty_selection(self):
        item = sample(); item["duration_hours"] = 0.1
        with self.assertRaisesRegex(ValueError, "Duration"):
            normalize_plan(item)
        item = sample(); item["machines"] = []
        with self.assertRaisesRegex(ValueError, "Select"):
            normalize_plan(item)

    def test_api_reports_validation_error(self):
        client = operations_app.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = "test-token"
        with patch.object(operations_app, "current_user", return_value={"id": 1, "username": "test", "role": "admin"}), \
                patch.object(operations_app, "pg", return_value=nullcontext()), \
                patch.object(operations_app, "save_plan", side_effect=ValueError("PIC is required")):
            response = client.post("/api/work-plans", json=sample(), headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("PIC", response.json["error"])


if __name__ == "__main__":
    unittest.main()
