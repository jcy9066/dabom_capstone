from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class PatrolRouteError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.error_code = code
        self.status_code = status_code


class PatrolRouteApi:
    """Persistent waypoint routes + sequential NavigateToPose orchestration."""

    TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELED"}

    def __init__(
        self,
        app: FastAPI,
        navigation_control: Any,
        csrf_failure: Callable[[Request], JSONResponse | None],
        store_path: Path,
        *,
        max_retries: int = 2,
        poll_interval_sec: float = 0.25,
    ) -> None:
        self._app = app
        self._navigation = navigation_control
        self._csrf_failure = csrf_failure
        self._store_path = Path(store_path)
        self._max_retries = max(0, int(max_retries))
        self._poll_interval_sec = max(0.1, float(poll_interval_sec))
        self._routes: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None
        self._paused = False
        self._stop_requested = False
        self._state: dict[str, Any] = {
            "state": "IDLE",
            "route_id": None,
            "route_name": None,
            "waypoint_index": None,
            "waypoint_count": 0,
            "loop_count": 0,
            "retry_count": 0,
            "last_error": None,
            "updated_at": time.time(),
        }
        self._load()
        self._attach_routes()

    def _load(self) -> None:
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if isinstance(raw, dict):
            routes = raw.get("routes", raw)
            if isinstance(routes, dict):
                self._routes = {
                    str(key): value
                    for key, value in routes.items()
                    if isinstance(value, dict)
                }

    def _save(self) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"routes": self._routes}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._store_path)

    @staticmethod
    def _finite(value: Any, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PatrolRouteError("INVALID_WAYPOINT", f"{field} must be numeric.") from exc
        if not math.isfinite(number):
            raise PatrolRouteError("INVALID_WAYPOINT", f"{field} must be finite.")
        return number

    def _normalize_route(self, payload: dict[str, Any], existing_id: str | None = None) -> dict[str, Any]:
        raw_waypoints = payload.get("waypoints")
        if not isinstance(raw_waypoints, list) or not raw_waypoints:
            raise PatrolRouteError("WAYPOINT_REQUIRED", "At least one waypoint is required.")
        if len(raw_waypoints) > 100:
            raise PatrolRouteError("TOO_MANY_WAYPOINTS", "A route can contain at most 100 waypoints.")

        waypoints = []
        for index, item in enumerate(raw_waypoints):
            if not isinstance(item, dict):
                raise PatrolRouteError("INVALID_WAYPOINT", f"Waypoint {index + 1} must be an object.")
            wait_sec = self._finite(item.get("wait_sec", 3.0), "wait_sec")
            if wait_sec < 0 or wait_sec > 300:
                raise PatrolRouteError("INVALID_WAYPOINT", "wait_sec must be between 0 and 300.")
            waypoints.append({
                "x": self._finite(item.get("x"), "x"),
                "y": self._finite(item.get("y"), "y"),
                "yaw": self._finite(item.get("yaw", 0.0), "yaw"),
                "wait_sec": wait_sec,
            })

        route_id = str(existing_id or payload.get("route_id") or uuid.uuid4().hex)
        name = str(payload.get("name") or "Patrol Route").strip()[:80] or "Patrol Route"
        map_name = str(payload.get("map_name") or "").strip()[:120] or None
        return {
            "route_id": route_id,
            "name": name,
            "map_name": map_name,
            "loop": bool(payload.get("loop", True)),
            "waypoints": waypoints,
            "updated_at": time.time(),
        }

    def _access(self, request: Request, csrf: bool = False):
        if not request.session.get("user"):
            return self._error("AUTH_REQUIRED", "Login is required.", 401)
        return self._csrf_failure(request) if csrf else None

    @staticmethod
    def _error(code: str, message: str, status: int):
        return JSONResponse(
            {"ok": False, "error_code": code, "error": message},
            status_code=status,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            **self._state,
            "paused": self._paused,
            "running": self._task is not None and not self._task.done(),
        }

    def _touch(self, **updates: Any) -> None:
        self._state.update(updates)
        self._state["updated_at"] = time.time()

    def _validate_active_map(self, route: dict[str, Any]) -> None:
        route_map = route.get("map_name")
        if not route_map:
            return
        control = self._navigation.state_response()
        active = control.get("active_map")
        active_name = active.get("name") if isinstance(active, dict) else None
        if active_name and str(active_name) != str(route_map):
            raise PatrolRouteError(
                "ROUTE_MAP_MISMATCH",
                f"Route requires map '{route_map}', active map is '{active_name}'.",
                409,
            )

    async def _wait_navigation_terminal(self) -> str:
        while not self._stop_requested:
            if self._paused:
                return "PAUSED"
            state = self._navigation.state_response()
            if state.get("emergency_stop"):
                self._paused = True
                self._touch(state="PAUSED", last_error="EMERGENCY_STOP_ACTIVE")
                return "PAUSED"
            nav_state = str(state.get("navigation_state") or "").upper()
            ros_state = str((state.get("ros_navigation") or {}).get("state") or "").upper()
            terminal = nav_state if nav_state in self.TERMINAL_STATES else ros_state
            if terminal in self.TERMINAL_STATES:
                return terminal
            await asyncio.sleep(self._poll_interval_sec)
        return "STOPPED"

    async def _run_route(self, route: dict[str, Any]) -> None:
        waypoints = list(route["waypoints"])
        loop_count = 0
        try:
            while not self._stop_requested:
                for index, waypoint in enumerate(waypoints):
                    if self._stop_requested:
                        break
                    while self._paused and not self._stop_requested:
                        await asyncio.sleep(self._poll_interval_sec)
                    if self._stop_requested:
                        break

                    retry = 0
                    while retry <= self._max_retries and not self._stop_requested:
                        self._touch(
                            state="PLANNING",
                            waypoint_index=index,
                            waypoint_count=len(waypoints),
                            loop_count=loop_count,
                            retry_count=retry,
                            last_error=None,
                        )
                        try:
                            await self._navigation.plan_goal({"goal": waypoint})
                            await self._navigation.start_navigation()
                            self._touch(state="NAVIGATING")
                            terminal = await self._wait_navigation_terminal()
                        except Exception as exc:
                            terminal = "FAILED"
                            self._touch(last_error=str(exc))

                        if terminal == "SUCCEEDED":
                            self._touch(state="WAITING", retry_count=retry)
                            wait_until = time.monotonic() + float(waypoint.get("wait_sec", 0))
                            while (
                                time.monotonic() < wait_until
                                and not self._stop_requested
                                and not self._paused
                            ):
                                await asyncio.sleep(min(self._poll_interval_sec, 0.25))
                            break
                        if terminal in {"PAUSED", "STOPPED"}:
                            while self._paused and not self._stop_requested:
                                await asyncio.sleep(self._poll_interval_sec)
                            if self._stop_requested:
                                break
                            retry = 0
                            continue

                        retry += 1
                        self._touch(
                            state="RETRYING" if retry <= self._max_retries else "SKIPPING",
                            retry_count=retry,
                            last_error=self._state.get("last_error") or terminal,
                        )

                    if self._stop_requested:
                        break

                if self._stop_requested:
                    break
                loop_count += 1
                self._touch(loop_count=loop_count)
                if not route.get("loop", True):
                    break

            self._touch(
                state="STOPPED" if self._stop_requested else "COMPLETED",
                waypoint_index=None,
                retry_count=0,
            )
        except asyncio.CancelledError:
            self._touch(state="STOPPED")
            raise
        except Exception as exc:
            self._touch(state="FAILED", last_error=str(exc))
        finally:
            self._task = None
            self._paused = False
            self._stop_requested = False

    async def start_route(self, route_id: str) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            raise PatrolRouteError("PATROL_ALREADY_RUNNING", "A patrol route is already running.", 409)
        route = self._routes.get(route_id)
        if route is None:
            raise PatrolRouteError("ROUTE_NOT_FOUND", "Patrol route not found.", 404)
        self._validate_active_map(route)

        control = self._navigation.state_response()
        if str(control.get("navigation_mode") or "").upper() != "DRIVING":
            raise PatrolRouteError("DRIVING_MODE_REQUIRED", "Load a map in DRIVING mode before patrol.", 409)
        if str(control.get("robot_mode") or "").lower() != "auto":
            raise PatrolRouteError("AUTO_MODE_REQUIRED", "Switch the robot to AUTO before patrol.", 409)

        self._stop_requested = False
        self._paused = False
        self._touch(
            state="STARTING",
            route_id=route_id,
            route_name=route["name"],
            waypoint_index=0,
            waypoint_count=len(route["waypoints"]),
            loop_count=0,
            retry_count=0,
            last_error=None,
        )
        self._task = asyncio.create_task(self._run_route(route), name=f"patrol-route-{route_id}")
        return self.snapshot()

    async def stop_route(self) -> dict[str, Any]:
        self._stop_requested = True
        self._paused = False
        task = self._task
        if task is not None and not task.done():
            try:
                await self._navigation.cancel_goal()
            except Exception:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._touch(state="STOPPED", waypoint_index=None)
        return self.snapshot()

    async def pause_route(self) -> dict[str, Any]:
        if self._task is None or self._task.done():
            raise PatrolRouteError("PATROL_NOT_RUNNING", "No patrol route is running.", 409)
        self._paused = True
        try:
            await self._navigation.cancel_goal()
        except Exception:
            pass
        self._touch(state="PAUSED")
        return self.snapshot()

    async def resume_route(self) -> dict[str, Any]:
        if self._task is None or self._task.done():
            raise PatrolRouteError("PATROL_NOT_RUNNING", "No patrol route is running.", 409)
        control = self._navigation.state_response()
        if control.get("emergency_stop"):
            raise PatrolRouteError("EMERGENCY_STOP_ACTIVE", "Release emergency stop before patrol resume.", 409)
        self._paused = False
        self._touch(state="RESUMING", last_error=None)
        return self.snapshot()

    async def _json(self, request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise PatrolRouteError("INVALID_REQUEST", "JSON object required.") from exc
        if not isinstance(payload, dict):
            raise PatrolRouteError("INVALID_REQUEST", "JSON object required.")
        return payload

    async def _mutation(self, request: Request, action):
        denied = self._access(request, csrf=True)
        if denied:
            return denied
        try:
            return await action()
        except PatrolRouteError as exc:
            return self._error(exc.error_code, str(exc), exc.status_code)
        except Exception as exc:
            return self._error("PATROL_FAILED", str(exc), 500)

    def _attach_routes(self) -> None:
        @self._app.get("/api/navigation/patrol/routes")
        async def list_routes(request: Request):
            denied = self._access(request)
            if denied:
                return denied
            return {"ok": True, "routes": list(self._routes.values())}

        @self._app.get("/api/navigation/patrol/state")
        async def patrol_state(request: Request):
            denied = self._access(request)
            return denied if denied else self.snapshot()

        @self._app.post("/api/navigation/patrol/routes")
        async def save_route(request: Request):
            async def action():
                payload = await self._json(request)
                route = self._normalize_route(payload, payload.get("route_id"))
                self._routes[route["route_id"]] = route
                self._save()
                return {"ok": True, "route": route}
            return await self._mutation(request, action)

        @self._app.delete("/api/navigation/patrol/routes/{route_id}")
        async def delete_route(route_id: str, request: Request):
            async def action():
                if route_id not in self._routes:
                    raise PatrolRouteError("ROUTE_NOT_FOUND", "Patrol route not found.", 404)
                if self._state.get("route_id") == route_id and self._task is not None:
                    raise PatrolRouteError("ROUTE_IN_USE", "Stop the active patrol before deleting it.", 409)
                deleted = self._routes.pop(route_id)
                self._save()
                return {"ok": True, "deleted": deleted}
            return await self._mutation(request, action)

        @self._app.post("/api/navigation/patrol/start")
        async def start_patrol(request: Request):
            async def action():
                payload = await self._json(request)
                route_id = str(payload.get("route_id") or "")
                return await self.start_route(route_id)
            return await self._mutation(request, action)

        @self._app.post("/api/navigation/patrol/stop")
        async def stop_patrol(request: Request):
            return await self._mutation(request, self.stop_route)

        @self._app.post("/api/navigation/patrol/pause")
        async def pause_patrol(request: Request):
            return await self._mutation(request, self.pause_route)

        @self._app.post("/api/navigation/patrol/resume")
        async def resume_patrol(request: Request):
            return await self._mutation(request, self.resume_route)
