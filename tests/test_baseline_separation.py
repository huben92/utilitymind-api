"""Current simulator readings must never overwrite configured baselines."""

from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import error as urlerror, request as urlrequest
from http.server import ThreadingHTTPServer

import app
import operations_app


class BaselineSeparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path = app.DB_PATH
        app.DB_PATH = Path(self.directory.name) / "simulator.sqlite3"

    def tearDown(self):
        app.DB_PATH = self.previous_path
        self.directory.cleanup()

    def test_existing_simulator_database_gets_separate_current_value(self):
        with sqlite3.connect(app.DB_PATH) as conn:
            conn.execute("""CREATE TABLE sensors (id TEXT PRIMARY KEY,area TEXT,metric TEXT,unit TEXT,
                primary_utility INTEGER,active INTEGER,min_value REAL,max_value REAL,
                baseline REAL,variance REAL,created_at TEXT)""")
            conn.execute("INSERT INTO sensors VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         ("legacy", "Area", "Electricity", "kW", 1, 1, 0, 100, 50, 8, "now"))
        app.init_db()
        with app.db() as conn:
            row = conn.execute("SELECT baseline,current_value FROM sensors WHERE id='legacy'").fetchone()
        self.assertEqual((row["baseline"], row["current_value"]), (50, 50))

    def test_slider_patch_changes_only_current_value(self):
        app.init_db()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/sensors/raw-material-warehouse--electricity"
        try:
            body = json.dumps({"current_value": 70}).encode()
            with urlrequest.urlopen(urlrequest.Request(url, body, {"Content-Type": "application/json"}, method="PATCH")) as response:
                self.assertEqual(response.status, 200)
            with app.db() as conn:
                row = conn.execute("SELECT baseline,current_value FROM sensors WHERE id=?",
                                   ("raw-material-warehouse--electricity",)).fetchone()
            self.assertEqual(row["current_value"], 70)
            self.assertNotEqual(row["baseline"], 70)
            body = json.dumps({"current_value": 600}).encode()
            with urlrequest.urlopen(urlrequest.Request(url, body, {"Content-Type": "application/json"}, method="PATCH")) as response:
                self.assertEqual(response.status, 200)
            with app.db() as conn:
                row = conn.execute("SELECT baseline,current_value FROM sensors WHERE id=?",
                                   ("raw-material-warehouse--electricity",)).fetchone()
            self.assertEqual(row["current_value"], 600)
            self.assertNotEqual(row["baseline"], 600)
            body = json.dumps({"baseline": 70}).encode()
            with self.assertRaises(urlerror.HTTPError) as rejection:
                urlrequest.urlopen(urlrequest.Request(url, body, {"Content-Type": "application/json"}, method="PATCH"))
            self.assertEqual(rejection.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_catalog_baseline_update_preserves_current_slider(self):
        app.init_db()
        sensor_id = "raw-material-warehouse--electricity"
        with app.db() as conn:
            conn.execute("UPDATE sensors SET current_value=70 WHERE id=?", (sensor_id,))
        catalog = {"sensors": [{"id": sensor_id, "machine_name": "Raw Material Warehouse",
                               "type": "Electricity", "unit": "kW", "enabled": True,
                               "min_value": 0, "max_value": 500, "baseline": 50}]}
        with patch.object(app.urlrequest, "urlopen", return_value=BytesIO(json.dumps(catalog).encode())):
            app.SYNC_LAST = 0
            app.sync_dashboard_sensors()
        with app.db() as conn:
            row = conn.execute("SELECT baseline,current_value FROM sensors WHERE id=?", (sensor_id,)).fetchone()
        self.assertEqual((row["baseline"], row["current_value"]), (50, 70))

    def test_dashboard_ignores_baseline_in_legacy_mqtt_payload(self):
        class FakeConnection:
            queries = []
            def execute(self, sql, params):
                self.queries.append(sql)
                return self
            def fetchone(self):
                return {"id": "meter", "machine_id": 1, "name": "Electricity 01", "type": "Electricity",
                        "unit": "kW", "baseline": 50, "max_value": 100, "enabled": True,
                        "machine_name": "Machine", "location_name": "Hall"}
        connection = FakeConnection()
        @contextmanager
        def fake_pg():
            yield connection
        class FakeClickHouse:
            def __init__(self):
                self.rows = []
            def insert(self, _table, rows, column_names):
                self.rows.extend(rows)
        warehouse = FakeClickHouse()
        payload = {"sensor_id": "meter", "value": 70, "baseline": 999,
                   "timestamp": "2026-10-17T00:00:00Z", "simulated": True}
        message = type("Message", (), {"payload": json.dumps(payload).encode(), "topic": "factory/meter"})()
        with patch.object(operations_app, "pg", fake_pg), patch.object(operations_app, "ch", return_value=warehouse), \
                patch.object(operations_app, "update_condition_state", return_value={"maximum_since": None}), \
                patch.object(operations_app, "record_prolonged_maximum"), \
                patch.object(operations_app, "evaluate_machine_conditions"):
            operations_app.on_message(None, None, message)
        self.assertEqual(warehouse.rows[0][2], 70)
        self.assertTrue(any("INSERT INTO metering_alerts" in query for query in connection.queries))
        self.assertFalse(any("UPDATE sensors" in query for query in connection.queries))


if __name__ == "__main__":
    unittest.main()
