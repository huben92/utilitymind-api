"""The map dialog must keep each machine meter's history and unit separate."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import operations_app


class MachineMeteringTrendsTests(unittest.TestCase):
    def test_returns_every_meter_with_independent_series(self):
        now = datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        machine = {"id": 7, "name": "Compressor", "code": "CMP-7", "enabled": True,
                   "location_name": "Utility Room"}
        meters = [
            {"id": "electric-7", "name": "Power", "type": "Electricity", "unit": "kW",
             "baseline": 50, "max_value": 80, "enabled": True},
            {"id": "air-7", "name": "Air flow", "type": "Compressed Air", "unit": "m³/h",
             "baseline": 100, "max_value": 150, "enabled": True},
        ]

        class Connection:
            def execute(self, sql, _params):
                rows = (machine if "FROM machines" in sql else
                        [] if "FROM metering_baseline_profiles" in sql else meters)
                return SimpleNamespace(fetchone=lambda: rows if isinstance(rows, dict) else None,
                                       fetchall=lambda: rows if isinstance(rows, list) else [])

        @contextmanager
        def fake_pg():
            yield Connection()

        results = iter([
            [("electric-7", 70, now - timedelta(seconds=1)),
             ("air-7", 125, now - timedelta(seconds=1))],
            [(minute, "electric-7", 68.5), (minute, "air-7", 121.25)],
        ])
        fake_ch = SimpleNamespace(query=lambda *_args, **_kwargs: SimpleNamespace(result_rows=next(results)))
        with patch.object(operations_app, "current_user", return_value={"id": 1, "role": "viewer"}), \
             patch.object(operations_app, "pg", fake_pg), \
             patch.object(operations_app, "ch", return_value=fake_ch):
            response = operations_app.app.test_client().get("/api/machine/7/metering-trends")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["machine"]["name"], "Compressor")
        self.assertEqual(len(payload["meters"]), 2)
        self.assertEqual([(meter["unit"], meter["reading"]["value"], meter["series"][0]["value"])
                          for meter in payload["meters"]],
                         [("kW", 70, 68.5), ("m³/h", 125, 121.25)])


if __name__ == "__main__":
    unittest.main()
