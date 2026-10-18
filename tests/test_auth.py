import unittest
from unittest.mock import patch

import operations_app


class AuthGuardTests(unittest.TestCase):
    def setUp(self):
        self.client = operations_app.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-token"

    def test_dashboard_requires_login(self):
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 401)

    def test_only_super_admin_can_add_users(self):
        body = {"username": "another", "role": "viewer", "password": "long-test-password"}
        for role in ("viewer", "admin"):
            with self.subTest(role=role), patch.object(operations_app, "current_user", return_value={"id": 1, "username": "test", "role": role}):
                response = self.client.post("/api/users", json=body, headers={"X-CSRF-Token": "test-token"})
                self.assertEqual(response.status_code, 403)

    def test_csrf_and_read_only_role_are_enforced(self):
        with patch.object(operations_app, "current_user", return_value={"id": 1, "username": "test", "role": "viewer"}):
            self.assertEqual(self.client.post("/api/master/machines", json={}).status_code, 403)
            response = self.client.post("/api/master/machines", json={}, headers={"X-CSRF-Token": "test-token"})
            self.assertEqual(response.status_code, 403)

    def test_simulator_service_has_narrow_scope(self):
        with patch.object(operations_app, "service_token_matches", return_value=True), \
                patch.object(operations_app, "current_user", return_value=None):
            headers = {"Authorization": "Bearer test-service-token"}
            self.assertEqual(self.client.post("/api/users", json={}, headers=headers).status_code, 401)
            self.assertEqual(self.client.put("/api/master/machines", json={"id": "1", "enabled": True}, headers=headers).status_code, 401)
            self.assertEqual(self.client.put("/api/master/sensors", json={"id": "1", "baseline": 20}, headers=headers).status_code, 403)


if __name__ == "__main__":
    unittest.main()
