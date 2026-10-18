import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import ai_chat


class QuerySafetyTests(unittest.TestCase):
    def test_approved_selects(self):
        self.assertIn("SELECT", ai_chat.validate_sql("postgres", "SELECT s.id, m.name FROM sensors s JOIN machines m ON m.id=s.machine_id"))
        self.assertIn("SELECT", ai_chat.validate_sql("clickhouse", "SELECT sensor_id, avg(value) FROM telemetry GROUP BY sensor_id"))
        self.assertIn("SELECT", ai_chat.validate_sql("postgres", "SELECT pic FROM work_plans"))

    def test_rejects_mutations_and_private_tables(self):
        for database, statement in (
            ("postgres", "DELETE FROM sensors"),
            ("postgres", "SELECT * FROM app_settings"),
            ("postgres", "SELECT * FROM pg_catalog.pg_roles"),
            ("postgres", "SELECT * FROM sensors; SELECT * FROM machines"),
            ("clickhouse", "SELECT * FROM system.users"),
            ("clickhouse", "SELECT * FROM telemetry SETTINGS readonly=0"),
            ("clickhouse", "SELECT * FROM telemetry LIMIT 10 FORMAT JSON"),
            ("clickhouse", "SELECT * FROM file('/etc/passwd')"),
            ("clickhouse", "ALTER TABLE telemetry DELETE WHERE 1"),
        ):
            with self.subTest(statement=statement), self.assertRaises(ai_chat.ChatError):
                ai_chat.validate_sql(database, statement)

    def test_url_requires_https_or_local_http(self):
        self.assertEqual(ai_chat.validate_api_url("http://host.docker.internal:11434/v1/chat/completions"),
                         "http://host.docker.internal:11434/v1/chat/completions")
        for url in ("http://example.com/v1/chat/completions", "https://user:secret@example.com/v1/chat/completions",
                    "https://example.com/v1/models"):
            with self.subTest(url=url), self.assertRaises(ai_chat.ChatError):
                ai_chat.validate_api_url(url)


class FakeSettingsConnection:
    def __init__(self):
        self.rows = {}

    def execute(self, sql, args=()):
        if sql.startswith("SELECT"):
            return self
        if sql.startswith("DELETE"):
            self.rows.pop("ai_api_key", None)
        else:
            key = "ai_api_key" if "VALUES ('ai_api_key'" in sql else args[0]
            self.rows[key] = args[0] if key == "ai_api_key" else args[1]
        return self

    def fetchall(self):
        return [{"key": key, "value": value} for key, value in self.rows.items()]


class ChatFlowTests(unittest.TestCase):
    def test_retries_temporary_503_then_recovers(self):
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, *_args):
                return b'{"choices":[{"message":{"content":"ready"}}]}'
        class Opener:
            attempts = 0
            def open(self, request, timeout):
                self.attempts += 1
                if self.attempts < 3:
                    raise HTTPError(request.full_url, 503, "unavailable", {}, None)
                return Response()
        opener = Opener()
        with patch.object(ai_chat, "build_opener", return_value=opener), patch.object(ai_chat.time, "sleep") as sleep:
            result = ai_chat.call_model("https://example.com/v1/chat/completions", "test", "",
                                        [{"role": "user", "content": "test"}])
        self.assertEqual(result, "ready")
        self.assertEqual(opener.attempts, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_persistent_503_has_availability_message(self):
        class Opener:
            attempts = 0
            def open(self, request, timeout):
                self.attempts += 1
                raise HTTPError(request.full_url, 503, "unavailable", {}, None)
        opener = Opener()
        with patch.object(ai_chat, "build_opener", return_value=opener), patch.object(ai_chat.time, "sleep"):
            with self.assertRaisesRegex(ai_chat.ChatError, "temporarily unavailable.*HTTP 503.*after retries"):
                ai_chat.call_model("https://example.com/v1/chat/completions", "test", "",
                                   [{"role": "user", "content": "test"}])
        self.assertEqual(opener.attempts, 3)

    def test_gemini_request_uses_structured_response_format(self):
        class Response:
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, *_args):
                return json.dumps({"choices": [{"message": {"content": '{"action":"final","answer":"ready"}'}}]}).encode()
        class Opener:
            def open(self, request, timeout):
                body = json.loads(request.data)
                self_test.assertEqual(body["response_format"]["type"], "json_schema")
                self_test.assertEqual(body["model"], "gemini-flash-latest")
                return Response()
        self_test = self
        with patch.object(ai_chat, "build_opener", return_value=Opener()):
            content = ai_chat.call_model("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                "gemini-flash-latest", "test-key", [{"role": "user", "content": "test"}], structured=True)
        self.assertEqual(ai_chat.parse_action(content)["action"], "final")

    def test_recovers_json_from_prose_and_retries_plain_text(self):
        wrapped = 'Here is the result:\n```json\n{"action":"final","answer":"ready"}\n```'
        self.assertEqual(ai_chat.parse_action(wrapped)["answer"], "ready")
        calls = iter(["I can check that for you.",
            json.dumps({"action": "query", "queries": [{"database": "postgres", "sql": "SELECT name FROM machines"}]}),
            json.dumps({"action": "final", "answer": "Raw Material Warehouse exists [Q1].", "visuals": []})])
        result = {"columns": ["name"], "rows": [{"name": "Raw Material Warehouse"}], "row_count": 1, "truncated": False}
        with patch.object(ai_chat, "run_query", return_value=result) as query:
            answer = ai_chat.answer_chat("What raw material data exists?", [], ("https://example.com/v1/chat/completions", "test", ""),
                None, None, threading.Lock(), model_call=lambda *_: next(calls))
        self.assertIn("Raw Material Warehouse", answer["answer"])
        query.assert_called_once()

    def test_checks_catalog_before_declaring_raw_material_data_unavailable(self):
        calls = iter([
            json.dumps({"action": "final", "answer": "No raw material data is available."}),
            json.dumps({"action": "final", "answer": "Raw Material Warehouse has clean water metering [Q1].", "visuals": [
                {"kind": "table", "query_id": "Q1", "title": "Available metering"}]})
        ])
        result = {"columns": ["machine_name", "utility"],
                  "rows": [{"machine_name": "Raw Material Warehouse", "utility": "Clean Water"}],
                  "row_count": 1, "truncated": False}
        with patch.object(ai_chat, "run_query", return_value=result) as query:
            answer = ai_chat.answer_chat("What data available on raw material?", [],
                ("https://example.com/v1/chat/completions", "test", ""), None, None, threading.Lock(),
                model_call=lambda *_: next(calls))
        self.assertIn("clean water", answer["answer"].lower())
        self.assertEqual(answer["evidence"][0]["id"], "Q1")
        self.assertIn("FROM machines m", query.call_args.args[1])

    def test_key_is_encrypted_and_never_exposed(self):
        conn = FakeSettingsConnection()
        with tempfile.TemporaryDirectory() as folder, patch.object(ai_chat, "KEY_FILE", Path(folder) / "key"):
            public = ai_chat.save_settings(conn, {"api_url": "https://example.com/v1/chat/completions",
                "model": "test-model", "api_key": "test-secret"})
            self.assertEqual(public, {"api_url": "https://example.com/v1/chat/completions",
                "model": "test-model", "has_api_key": True})
            self.assertNotIn("test-secret", conn.rows["ai_api_key"])
            self.assertEqual(ai_chat.private_settings(conn)[2], "test-secret")

    def test_query_result_drives_visual_and_answer(self):
        calls = iter([
            json.dumps({"action": "query", "queries": [{"database": "postgres", "sql": "SELECT name, id FROM machines"}]}),
            json.dumps({"action": "final", "answer": "One machine is shown [Q1].", "visuals": [
                {"kind": "bar", "query_id": "Q1", "title": "Machines", "x": "name", "y": "id", "unit": "ID"},
                {"kind": "bar", "query_id": "Q9", "title": "Invented", "x": "name", "y": "id"}]})
        ])
        result = {"columns": ["name", "id"], "rows": [{"name": "Charging Area", "id": 2}], "row_count": 1, "truncated": False}
        with patch.object(ai_chat, "run_query", return_value=result) as query:
            answer = ai_chat.answer_chat("List machines", [], ("https://example.com/v1/chat/completions", "test", ""),
                None, None, threading.Lock(), model_call=lambda *_: next(calls))
        self.assertEqual(answer["answer"], "One machine is shown [Q1].")
        self.assertEqual(len(answer["visuals"]), 1)
        self.assertEqual(answer["evidence"][0]["result"]["rows"][0]["name"], "Charging Area")
        query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
