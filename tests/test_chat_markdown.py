import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from chat_markdown import render_ai_markdown
import operations_app


class ChatMarkdownTests(unittest.TestCase):
    def test_formats_common_ai_answer(self):
        rendered = render_ai_markdown(
            "## Electricity\n\n**Peak**: 70 kW\n\n"
            "- Furnace 1\n- Furnace 2\n\n"
            "| Machine | kW |\n| --- | ---: |\n| Furnace 1 | 70 |\n\n"
            "```sql\nSELECT 1;\n```\n"
        )
        self.assertIn("<h2>Electricity</h2>", rendered)
        self.assertIn("<strong>Peak</strong>", rendered)
        self.assertIn("<li>Furnace 1</li>", rendered)
        self.assertIn("<table>", rendered)
        self.assertIn("<pre><code>", rendered)

    def test_escapes_html_and_removes_unsafe_links(self):
        rendered = render_ai_markdown(
            '<img src=x onerror="alert(1)"> <script>alert(2)</script> '
            '[bad](javascript:alert(3)) [good](https://example.com/report)'
        )
        self.assertNotIn("<img", rendered)
        self.assertNotIn("<script", rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertNotIn('onerror=', rendered.replace("&quot;", '"').split("<a")[1])
        self.assertIn('href="https://example.com/report"', rendered)

    def test_chat_api_returns_safe_formatted_answer(self):
        answer = {"answer": "**70 kW** <script>alert(1)</script>", "visuals": [], "evidence": []}
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        conn.execute.return_value.fetchall.return_value = []
        client = operations_app.app.test_client()
        with client.session_transaction() as session:
            session["csrf_token"] = "test-token"
        with patch.object(operations_app, "current_user", return_value={"id": 1, "username": "test", "role": "viewer"}), \
                patch.object(operations_app, "pg", return_value=nullcontext(conn)), \
                patch.object(operations_app, "private_settings", return_value=("https://example.com", "test", "")), \
                patch.object(operations_app, "answer_chat", return_value=answer):
            response = client.post("/api/ai/chat", json={"question": "Usage?", "history": []}, headers={"X-CSRF-Token": "test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["answer"], answer["answer"])
        self.assertIn("<strong>70 kW</strong>", response.json["answer_html"])
        self.assertNotIn("<script", response.json["answer_html"])


if __name__ == "__main__":
    unittest.main()
