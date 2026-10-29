"""Isolated Initial Pose workflow; physical movement remains in NavigationControlApi.

The workflow never bypasses map occupancy, fresh scan, Pi STOP or AMCL checks.
A cancellation is accepted before the DRIVING transition, not during a
partially applied ROS2/AMCL transition (which must complete fail-closed).
"""
from __future__ import annotations

import asyncio
import math
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from server.navigation_control_api import NavigationControlError


class LocalizationSessionApi:
    WAITING = frozenset({"WAITING_FOR_CLICK", "POSE_CONFIRMATION_REQUIRED"})
    TERMINAL = frozenset({"READY", "FAILED", "CANCELLED", "IDLE"})

    def __init__(self, app: FastAPI, navigation: Any):
        self.navigation = navigation
        self.session_id = ""
        self.phase = "IDLE"
        self.source = None
        self.map_name = None
        self.map_revision = None
        self.reason = None
        self.error_code = None
        self.result = None
        self.proposed_pose = None
        self._task: asyncio.Task | None = None
        self._cancel_requested = False
        self._owner = None
        self._app = app
        self._attach_routes()

    def snapshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "session_id": self.session_id,
            "phase": self.phase,
            "source": self.source,
            "map_name": self.map_name,
            "map_revision": self.map_revision,
            "error_code": self.error_code,
            "reason": self.reason,
            "proposed_pose": self.proposed_pose,
            "localization_ready": (
                self.phase == "READY" and bool(self.result)
                and self.result.get("localization_ready") is True
                and self.result.get("nav2_ready") is True
            ),
            "can_cancel": self.phase in {
                "QUEUED", "ESTIMATING", *self.WAITING,
            },
        }

    def _error(self, code: str, message: str, status: int = 409):
        return JSONResponse(
            {"ok": False, "error_code": code, "error": message},
            status_code=status,
        )

    def _check_cancelled(self):
        if self._cancel_requested:
            raise NavigationControlError(
                "LOCALIZATION_CANCELLED", "Localization was cancelled.", 409,
            )

    def _validate_session(self, payload: dict[str, Any]):
        if payload.get("session_id") != self.session_id or not self.session_id:
            raise NavigationControlError(
                "LOCALIZATION_SESSION_CHANGED",
                "The selected localization session is no longer active.", 409,
            )

    async def start(self, payload: dict[str, Any], *, user: str) -> dict[str, Any]:
        source = str(payload.get("source") or "").strip().lower()
        if source not in {"existing", "current"}:
            raise NavigationControlError(
                "INVALID_SOURCE", "source must be existing or current", 400,
            )
        map_name = payload.get("map_name")
        if source == "existing" and (not isinstance(map_name, str) or not map_name.strip()):
            raise NavigationControlError("MAP_REQUIRED", "Select a saved map", 400)
        if self.phase not in self.TERMINAL:
            raise NavigationControlError(
                "LOCALIZATION_BUSY", "Cancel or finish the current localization first.",
            )
        if self.navigation._operation_lock.locked():
            raise NavigationControlError(
                "NAVIGATION_BUSY", "Another navigation operation is in progress.",
            )
        task = getattr(self.navigation, "_loop_session_owner", None)
        if task is not None and not task.done():
            raise NavigationControlError(
                "LOOP_BUSY", "Stop the current Loop before localization.",
            )
        self.session_id = uuid4().hex
        self.source = source
        self.map_name = map_name if source == "existing" else None
        self.map_revision = payload.get("map_revision")
        self.phase = "QUEUED"
        self.reason = self.error_code = self.result = self.proposed_pose = None
        self._cancel_requested = False
        self._owner = user
        self._task = asyncio.create_task(self._estimate_and_apply(self.session_id))
        return self.snapshot()

    async def _prepare_map(self):
        self._check_cancelled()
        if self.source == "existing":
            selected = await asyncio.to_thread(
                self.navigation._map_api.resolve_map, self.map_name,
            )
            preview = await asyncio.to_thread(
                self.navigation._map_api._registry.preview_map, selected.map_name,
            )
            revision = preview["map_revision"]
            if self.map_revision is not None and self.map_revision != revision:
                raise NavigationControlError(
                    "MAP_REVISION_CHANGED", "The saved map changed.", 409,
                )
            self.map_revision = revision
            return selected, None

        live = self.navigation._get_live_map()
        if (not isinstance(live, dict)
            or live.get("source") != "slam_toolbox"
            or live.get("map_verified") is not True
            or live.get("mapping_state") != "MAPPING_ACTIVE"):
            raise NavigationControlError(
                "MAPPING_NOT_READY", "An active, verified SLAM map is required.", 409,
            )
        revision = live.get("map_revision")
        if not revision or (self.map_revision is not None and self.map_revision != revision):
            raise NavigationControlError(
                "MAP_REVISION_CHANGED", "The current SLAM map changed.", 409,
            )
        self.map_revision = revision
        self._check_cancelled()
        saved = await asyncio.to_thread(
            self.navigation._save_map, dict(live), None,
        )
        selected = await asyncio.to_thread(
            self.navigation._map_api.resolve_map, saved["map_name"],
        )
        self.map_name = selected.map_name
        return selected, live

    async def _estimate_and_apply(self, session_id: str):
        try:
            async with self.navigation._operation_lock:
                self._check_cancelled()
                self.phase = "ESTIMATING"
                selected, live = await self._prepare_map()
                self._check_cancelled()
                if live is not None:
                    pose = self.navigation._current_mapping_initial_pose()
                else:
                    pose = None
                if pose is None:
                    pose, _ = await self.navigation._estimate_initial_pose_with_retry(
                        selected,
                        expected_map_revision=(self.map_revision if live is not None else None),
                        cancelled=lambda: self._cancel_requested,
                    )
                self._check_cancelled()
                await self._validate_and_apply(selected, pose, live)
        except Exception as exc:
            self._handle_error(exc, session_id)

    def _validate_map_revision(self, live):
        if self.source == "existing":
            current = self.navigation._map_api._registry.preview_map(self.map_name)
            actual = current["map_revision"]
        else:
            current = self.navigation._get_live_map() or {}
            actual = current.get("map_revision")
            if current.get("mapping_state") != "MAPPING_ACTIVE":
                raise NavigationControlError(
                    "MAPPING_NOT_READY", "Mapping became unhealthy.", 409,
                )
        if actual != self.map_revision:
            raise NavigationControlError(
                "MAP_REVISION_CHANGED", "Map changed during localization.", 409,
            )

    async def _validate_and_apply(self, selected, pose, live):
        self._check_cancelled()
        self._validate_map_revision(live)
        self.proposed_pose = {
            key: pose[key] for key in ("x", "y", "yaw_degrees")
        }
        if pose.get("warning_acknowledged") is True:
            self.proposed_pose["warning_acknowledged"] = True
        if live is not None:
            await asyncio.to_thread(
                self.navigation._validate_live_initial_pose, live, self.proposed_pose,
            )
        await asyncio.to_thread(
            self.navigation._map_api.validate_initial_pose,
            selected, self.proposed_pose,
        )
        self._check_cancelled()
        self.phase = "APPLYING"
        request = {
            "mode": "DRIVING", "source": self.source,
            "map_revision": self.map_revision,
            "initial_pose": self.proposed_pose,
        }
        if self.source == "existing":
            request["map_name"] = self.map_name
        # This call revalidates the map and waits for AMCL/Nav2 + Pi.
        result = await self.navigation.switch_mode(request, user=self._owner)
        if (result.get("localization_ready") is not True
            or result.get("nav2_ready") is not True):
            raise NavigationControlError(
                "LOCALIZATION_UNVERIFIED",
                "AMCL and Nav2 did not confirm localization readiness.", 409,
            )
        self.result = result
        self.phase = "READY"
        self.reason = self.error_code = None

    async def pose(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_session(payload)
        if self.phase not in self.WAITING:
            raise NavigationControlError(
                "LOCALIZATION_NOT_WAITING_FOR_POSE",
                "Map-click recovery is not currently requested.", 409,
            )
        raw = payload.get("initial_pose")
        if not isinstance(raw, dict):
            raise NavigationControlError("INVALID_INITIAL_POSE", "Pose object required.", 400)
        try:
            selected = {
                "x": float(raw["x"]), "y": float(raw["y"]),
                "yaw_degrees": float(raw["yaw_degrees"]),
            }
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise NavigationControlError(
                "INVALID_INITIAL_POSE", "Finite x, y, and yaw required.", 400,
            ) from exc
        if not all(math.isfinite(x) for x in selected.values()):
            raise NavigationControlError("INVALID_INITIAL_POSE", "Pose must be finite.", 400)
        if raw.get("warning_acknowledged") is True:
            selected["warning_acknowledged"] = True
        if self.navigation._operation_lock.locked():
            raise NavigationControlError(
                "NAVIGATION_BUSY", "Another navigation operation is in progress.",
            )
        self.proposed_pose = selected
        self.phase = "QUEUED"
        self.reason = self.error_code = None
        self._task = asyncio.create_task(self._apply_manual_pose(self.session_id, selected))
        return self.snapshot()

    async def _apply_manual_pose(self, session_id: str, pose: dict):
        try:
            async with self.navigation._operation_lock:
                self._check_cancelled()
                self.phase = "POSE_VALIDATING"
                selected = await asyncio.to_thread(
                    self.navigation._map_api.resolve_map, self.map_name,
                )
                live = (self.navigation._get_live_map()
                        if self.source == "current" else None)
                await self._validate_and_apply(selected, pose, live)
        except Exception as exc:
            self._handle_error(exc, session_id)

    def _handle_error(self, exc: Exception, session_id: str):
        if session_id != self.session_id:
            return
        code = getattr(exc, "error_code", type(exc).__name__)
        self.error_code = str(code)
        self.reason = str(exc)[:300]
        if self._cancel_requested or code == "LOCALIZATION_CANCELLED":
            self.phase = "CANCELLED"
        elif code == "INITIAL_POSE_CONFIRMATION_REQUIRED":
            self.phase = "POSE_CONFIRMATION_REQUIRED"
        elif code in {
            "AWAITING_MAP_CLICK", "AUTO_POSE_REQUIRES_STOP",
            "INITIAL_POSE_OCCUPIED", "INVALID_INITIAL_POSE",
        }:
            self.phase = "WAITING_FOR_CLICK"
        else:
            self.phase = "FAILED"

    async def cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate_session(payload)
        if self.phase in {"APPLYING", "READY"}:
            raise NavigationControlError(
                "LOCALIZATION_ALREADY_APPLYING",
                "AMCL activation cannot be interrupted; use navigation STOP.",
                409,
            )
        if self.phase == "CANCELLED":
            return self.snapshot()
        self._cancel_requested = True
        self.phase = "CANCELLED"
        self.proposed_pose = None
        self.reason = "Cancelled by operator before Driving activation."
        return self.snapshot()

    def _attach_routes(self):
        @self._app.get("/api/navigation/localization/state")
        async def state(request: Request):
            denied = self.navigation._access(request)
            return denied if denied else self.snapshot()

        async def invoke(request, operation, *, with_user=False):
            denied = self.navigation._access(request, csrf=True)
            if denied:
                return denied
            try:
                payload = await request.json()
            except (ValueError, TypeError):
                payload = {}
            if not isinstance(payload, dict):
                return self._error("INVALID_REQUEST", "JSON object required.", 400)
            try:
                if with_user:
                    user = request.session.get("user") or {}
                    identity = str(user.get("user_id") or user.get("email") or "unknown")
                    return await operation(payload, user=identity)
                return await operation(payload)
            except NavigationControlError as exc:
                return self._error(exc.error_code, str(exc), exc.status_code)

        @self._app.post("/api/navigation/localization/start")
        async def start(request: Request):
            return await invoke(request, self.start, with_user=True)

        @self._app.post("/api/navigation/localization/pose")
        async def pose(request: Request):
            return await invoke(request, self.pose)

        @self._app.post("/api/navigation/localization/cancel")
        async def cancel(request: Request):
            return await invoke(request, self.cancel)
