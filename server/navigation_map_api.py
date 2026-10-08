"""FastAPI integration for saved-map loading and AMCL initial pose resets."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.navigation_map_service import NavigationMapError, NavigationMapService
from server.navigation_ros_control import NavigationRosControl, NavigationRosError


class NavigationMapApi:
    def __init__(
        self,
        app: FastAPI,
        root_dir: Path,
        map_dir: Path,
        csrf_failure: Callable[[Request], JSONResponse | None],
    ) -> None:
        self._app = app
        self._registry = NavigationMapService(root_dir, map_dir)
        self._ros = NavigationRosControl()
        self._csrf_failure = csrf_failure
        self._load_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._operation_state = "idle"
        self._active_listener: Callable[[dict[str, Any]], None] | None = None
        self._control_loader: Callable[..., Any] | None = None
        self._logger = logging.getLogger(__name__)
        self._attach_routes()

    def start(self) -> bool:
        return self._ros.start()

    def close(self) -> None:
        self._ros.close()

    @property
    def ros_control(self) -> NavigationRosControl:
        return self._ros

    def resolve_map(self, map_name: Any):
        return self._registry.get_map(map_name)

    def set_active_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._active_listener = listener

    def set_control_loader(self, loader: Callable[..., Any]) -> None:
        self._control_loader = loader

    def set_visualization_listener(
        self,
        listener: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self._ros.set_visualization_listener(listener)

    async def activate_map(self, payload: dict[str, Any], user: str = "navigation_control") -> dict[str, Any]:
        """Load a saved map through the same serialized path used by the dashboard."""
        if not self._load_lock.acquire(blocking=False):
            raise NavigationMapError(
                "MAP_LOAD_IN_PROGRESS",
                "Another map load request is already in progress.",
                409,
            )
        try:
            return await asyncio.to_thread(self._activate_map_locked, payload, user)
        finally:
            self._load_lock.release()
            with self._state_lock:
                if self._operation_state != "active":
                    self._operation_state = "idle"

    async def list_maps(self, request: Request):
        denied = self._login_failure(request)
        if denied:
            return denied
        active = await asyncio.to_thread(self.active_response)
        active_name = (active.get("active_map") or {}).get("map_name")
        maps = self._registry.list_maps()
        for saved_map in maps:
            saved_map["active"] = saved_map["map_name"] == active_name
        return {"ok": True, "maps": maps, "state": active["state"]}

    def active_response(self) -> dict[str, Any]:
        saved = self._registry.saved_state()
        active = saved.get("active_map")
        selected = None
        if isinstance(active, dict) and isinstance(active.get("map_name"), str):
            try:
                selected = self._registry.get_map(active["map_name"])
            except NavigationMapError:
                active = None
        ros = self._ros.active_status(selected)
        with self._state_lock:
            operation_state = self._operation_state

        is_active = bool(
            active
            and ros.get("map_server") == "active"
            and ros.get("amcl") == "active"
            and ros.get("map_verified")
        )
        if operation_state in {"loading", "resetting_pose", "verifying"}:
            state = operation_state
        elif is_active:
            state = "active"
        elif not ros.get("available"):
            state = "unavailable"
        else:
            state = "checking"
        return {
            "ok": True,
            "state": state,
            "active_map": active if is_active else None,
            "last_selected_map": saved.get("last_selected_map"),
            "ros": ros,
        }

    async def load_map(self, request: Request):
        denied = self._login_failure(request)
        if denied:
            return denied
        csrf_error = self._csrf_failure(request)
        if csrf_error:
            return csrf_error
        try:
            payload = await request.json()
        except Exception:
            return self._error("INVALID_REQUEST", "Map load request must be JSON.")
        if not isinstance(payload, dict):
            return self._error("INVALID_REQUEST", "Map load request must be an object.")
        if self._control_loader is None:
            return self._error(
                "NAVIGATION_CONTROL_REQUIRED",
                "Saved maps must be loaded through the DRIVING mode transition.",
                409,
            )
        try:
            user = request.session.get("user") or {}
            identity = str(user.get("user_id") or user.get("email") or "unknown")
            return await self._control_loader(payload, user=identity)
        except (NavigationMapError, NavigationRosError) as exc:
            return self._error(exc.error_code, str(exc), exc.status_code)
        except Exception:
            self._logger.exception("navigation map load failed")
            return self._error("UNDEFINED_FAILURE", "The selected map could not be loaded.", 500)

    async def rename_map(self, request: Request):
        denied = self._login_failure(request)
        if denied:
            return denied
        csrf_error = self._csrf_failure(request)
        if csrf_error:
            return csrf_error
        try:
            payload = await request.json()
        except Exception:
            return self._error("INVALID_REQUEST", "Map rename request must be JSON.")
        if not isinstance(payload, dict):
            return self._error("INVALID_REQUEST", "Map rename request must be an object.")
        if not self._load_lock.acquire(blocking=False):
            return self._error(
                "MAP_OPERATION_IN_PROGRESS",
                "Another saved-map operation is already in progress.",
                409,
            )
        try:
            return await asyncio.to_thread(self._rename_map_locked, request, payload)
        except NavigationMapError as exc:
            return self._error(exc.error_code, str(exc), exc.status_code)
        except Exception:
            self._logger.exception("navigation map rename failed")
            return self._error("UNDEFINED_FAILURE", "The selected map could not be renamed.", 500)
        finally:
            self._load_lock.release()

    def _rename_map_locked(self, request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        renamed_map = self._registry.rename_map(payload.get("map_name"), payload.get("new_map_name"))
        user = request.session.get("user") or {}
        self._logger.info(
            "navigation map renamed user=%s map=%s",
            user.get("user_id", user.get("email", "unknown")),
            renamed_map.map_name,
        )
        return {"ok": True, "map": renamed_map.public_dict(self._registry.root_dir)}

    def _activate_map_locked(self, payload: dict[str, Any], user: str) -> dict[str, Any]:
        selected_map = self._registry.get_map(payload.get("map_name"))
        initial_pose = self._registry.validate_initial_pose(selected_map, payload.get("initial_pose"))
        self._registry.mark_selected(selected_map)
        self._logger.info(
            "navigation map load requested user=%s map=%s yaml=%s",
            user,
            selected_map.map_name,
            selected_map.yaml_path,
        )
        self._set_operation_state("loading")
        result = self._ros.load_map_and_reset_pose(
            selected_map,
            initial_pose,
            progress=self._set_operation_state,
        )
        self._set_operation_state("verifying")
        active = self._registry.mark_active(selected_map, initial_pose, result["verification"])
        if self._active_listener is not None:
            self._active_listener(dict(active))
        with self._state_lock:
            self._operation_state = "active"
        return {
            "ok": True,
            "active_map": active,
            "initial_pose": {
                "x": initial_pose["x"],
                "y": initial_pose["y"],
                "yaw_degrees": initial_pose["yaw_degrees"],
                "yaw": initial_pose["yaw"],
            },
            "localization": result["localization"],
        }

    def _set_operation_state(self, value: str) -> None:
        with self._state_lock:
            self._operation_state = value

    def _attach_routes(self) -> None:
        @self._app.get("/api/navigation/maps/active")
        async def get_active_navigation_map(request: Request):
            denied = self._login_failure(request)
            if denied:
                return denied
            return await asyncio.to_thread(self.active_response)

        @self._app.post("/api/navigation/maps/rename")
        async def rename_saved_navigation_map(request: Request):
            return await self.rename_map(request)

        @self._app.post("/api/navigation/maps/load")
        async def load_saved_navigation_map(request: Request):
            return await self.load_map(request)

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
