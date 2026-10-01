import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from server.location_security import LocationSecurityService
from server.location_security_api import LocationSecurityApi


class LocationSecurityApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = FastAPI()

        @self.app.middleware("http")
        async def session(request, call_next):
            request.scope.setdefault("session", {})
            request.scope["session"]["user"] = {"user_id": "tester"}
            request.scope["session"]["csrf_token"] = "test-token"
            return await call_next(request)

        def csrf_failure(request):
            if request.headers.get("X-CSRF-Token") == "test-token":
                return None
            return JSONResponse({"ok": False, "error": "csrf"}, status_code=403)

        self.service = LocationSecurityService(Path(self.tmp.name) / "locations.json")
        self.api = LocationSecurityApi(self.app, self.service, csrf_failure)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_saved_locations_are_available_without_live_gps(self):
        response = self.client.post(
            "/api/navigation/locations",
            json={"name": "CtrlCV Lab", "lat": 37.0, "lng": 127.0, "radius_m": 30},
            headers={"X-CSRF-Token": "test-token"},
        )
        self.assertEqual(200, response.status_code)
        status = self.client.get("/api/navigation/locations").json()
        self.assertEqual("UNKNOWN", status["security_state"])
        self.assertEqual("CtrlCV Lab", status["locations"][0]["name"])

    def test_mutation_requires_csrf(self):
        response = self.client.post(
            "/api/navigation/locations",
            json={"name": "CtrlCV Lab", "lat": 37.0, "lng": 127.0, "radius_m": 30},
        )
        self.assertEqual(403, response.status_code)

    def test_robot_status_failure_is_isolated(self):
        self.api.note_robot_status({"gps_lat": "bad", "gps_lng": object()})
        self.assertEqual("UNKNOWN", self.service.snapshot()["security_state"])

    def test_robot_status_updates_live_fix(self):
        self.service.upsert_location(name="CtrlCV Lab", lat=37.0, lng=127.0, radius_m=30)
        result = self.api.note_robot_status(
            {"gps_fix": True, "gps_lat": 37.0, "gps_lng": 127.0, "gps_hdop": 1.0}
        )
        self.assertEqual("NORMAL", result["snapshot"]["security_state"])


if __name__ == "__main__":
    unittest.main()
