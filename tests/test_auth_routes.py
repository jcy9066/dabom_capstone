import importlib
import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ["INFERENCE_ENABLED"] = "false"
os.environ["VISUALIZATION_ENABLED"] = "false"
os.environ["MODEL_REQUIRED"] = "false"
os.environ["NAV_DRY_RUN_ENABLED"] = "true"
os.environ["COOKIE_SECURE"] = "false"


class AuthRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.routing import APIRoute
            from fastapi.testclient import TestClient
        except ImportError as exc:
            raise unittest.SkipTest(f"FastAPI test dependencies are unavailable: {exc}")
        cls.api_route_type = APIRoute
        cls.server = importlib.import_module("server.app")
        cls.client = TestClient(cls.server.app)

    def setUp(self):
        self.client.cookies.clear()

    def test_page_get_routes_are_registered_once(self):
        counts = {path: 0 for path in ("/", "/login", "/main")}
        for route in self.server.app.routes:
            if (
                isinstance(route, self.api_route_type)
                and route.path in counts
                and "GET" in route.methods
            ):
                counts[route.path] += 1
        self.assertEqual({"/": 1, "/login": 1, "/main": 1}, counts)

    def test_unauthenticated_page_redirects(self):
        root = self.client.get("/", follow_redirects=False)
        main = self.client.get("/main", follow_redirects=False)
        login = self.client.get("/login", follow_redirects=False)
        self.assertEqual(302, root.status_code)
        self.assertEqual("/login", root.headers["location"])
        self.assertEqual(302, main.status_code)
        self.assertEqual("/login", main.headers["location"])
        self.assertEqual(200, login.status_code)

    def test_login_access_and_logout_redirects(self):
        csrf = self.client.get("/api/auth/csrf").json()["csrf_token"]
        fake_user = {"user_id": 1, "email": "tester@example.com"}
        with patch.object(
            self.server,
            "call_auth",
            AsyncMock(return_value=(fake_user, None)),
        ):
            response = self.client.post(
                "/api/auth/login",
                headers={"X-CSRF-Token": csrf},
                json={"login_id": "tester@example.com", "password": "Password1"},
            )
        self.assertEqual(200, response.status_code)

        login = self.client.get("/login", follow_redirects=False)
        main = self.client.get("/main", follow_redirects=False)
        self.assertEqual(302, login.status_code)
        self.assertEqual("/main", login.headers["location"])
        self.assertEqual(200, main.status_code)

        csrf = self.client.get("/api/auth/csrf").json()["csrf_token"]
        logout = self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(200, logout.status_code)
        after_logout = self.client.get("/main", follow_redirects=False)
        self.assertEqual(302, after_logout.status_code)
        self.assertEqual("/login", after_logout.headers["location"])


if __name__ == "__main__":
    unittest.main()
