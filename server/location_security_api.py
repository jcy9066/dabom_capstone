"""FastAPI routes for optional saved GPS locations."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.location_security import LocationSecurityError, LocationSecurityService


class LocationSecurityApi:
    def __init__(
        self,
        app: FastAPI,
        service: LocationSecurityService,
        csrf_failure: Callable[[Request], JSONResponse | None],
    ) -> None:
        self._app = app
        self._service = service
        self._csrf_failure = csrf_failure
        self._logger = logging.getLogger(__name__)
        self._attach_routes()

    def snapshot(self) -> dict[str, Any]:
        return self._service.snapshot()

    async def get_status(self, request: Request):
        denied = self._login_failure(request)
        if denied:
            return denied
        return {"ok": True, **self._service.snapshot()}

    async def save_location(self, request: Request):
        denied = self._login_failure(request)
        if denied:
            return denied
        csrf_error = self._csrf_failure(request)
        if csrf_error:
            return csrf_error
        try:
            payload = await request.json()
        except Exception:
            return self._error("INVALID_REQUEST", "Location request must be JSON.")
        if not isinstance(payload, dict):
            return self._error("INVALID_REQUEST", "Location request must be an object.")
        try:
            location = self._service.upsert_location(
                location_id=payload.get("location_id"),
                name=payload.get("name"),
                lat=payload.get("lat"),
                lng=payload.get("lng"),
                radius_m=payload.get("radius_m", 30),
            )
        except LocationSecurityError as exc:
            return self._error(exc.error_code, str(exc))
        return {"ok": True, "location": location, "status": self._service.snapshot()}

    async def delete_location(self, request: Request, location_id: str):
        denied = self._login_failure(request)
        if denied:
            return denied
        csrf_error = self._csrf_failure(request)
        if csrf_error:
            return csrf_error
        deleted = self._service.delete_location(location_id)
        if not deleted:
            return self._error("LOCATION_NOT_FOUND", "Location does not exist.", 404)
        return {"ok": True, "status": self._service.snapshot()}

    def note_robot_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Consume optional GPS fields without affecting robot status ingestion."""
        sample = {
            "fix": payload.get("gps_fix"),
            "lat": payload.get("gps_lat"),
            "lng": payload.get("gps_lng"),
            "alt": payload.get("gps_alt"),
            "satellites": payload.get("gps_satellites"),
            "hdop": payload.get("gps_hdop"),
            "updated_at": payload.get("gps_updated_at"),
        }
        try:
            return self._service.update_live_gps(sample)
        except Exception:
            self._logger.exception("optional GPS status processing failed")
            return {"transition": None, "snapshot": self._service.snapshot()}

    def _attach_routes(self) -> None:
        @self._app.get("/api/navigation/locations")
        async def get_navigation_locations(request: Request):
            return await self.get_status(request)

        @self._app.post("/api/navigation/locations")
        async def save_navigation_location(request: Request):
            return await self.save_location(request)

        @self._app.delete("/api/navigation/locations/{location_id}")
        async def delete_navigation_location(request: Request, location_id: str):
            return await self.delete_location(request, location_id)

    @staticmethod
    def _error(error_code: str, message: str, status_code: int = 400):
        return JSONResponse(
            {"ok": False, "error_code": error_code, "error": message},
            status_code=status_code,
        )

    @staticmethod
    def _login_failure(request: Request):
        if request.session.get("user"):
            return None
        return JSONResponse(
            {"ok": False, "error_code": "AUTH_REQUIRED", "error": "Login is required."},
            status_code=401,
        )
