"""Localization session contracts without a running ROS2/Pi stack.

Real sensor, AMCL, and motor validation remains a separate final gate.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from server.localization_session_api import LocalizationSessionApi
from server.navigation_control_api import NavigationControlError


class Registry:
    @staticmethod
    def preview_map(name):
        return {"map_revision": "frozen-revision", "map_name": name}


class MapApi:
    def __init__(self):
        self._registry = Registry()

    @staticmethod
    def resolve_map(name):
        return SimpleNamespace(map_name=name)

    @staticmethod
    def validate_initial_pose(_selected, pose):
        if pose["x"] < 0:
            raise NavigationControlError(
                "INITIAL_POSE_OCCUPIED", "Occupied map position"
            )
        if pose["x"] < 1 and pose.get("warning_acknowledged") is not True:
            raise NavigationControlError(
                "INITIAL_POSE_CONFIRMATION_REQUIRED", "Unknown map cell"
            )


class Navigation:
    def __init__(self):
        self._operation_lock = asyncio.Lock()
        self._loop_session_owner = None
        self._map_api = MapApi()
        self.calls = []
        self.estimate_error = None
        self.live_map = {
            "source": "slam_toolbox", "map_verified": True,
            "mapping_state": "MAPPING_ACTIVE",
            "map_revision": "live-revision",
        }
        self.use_live_pose = False

    def _get_live_map(self):
        return dict(self.live_map)

    @staticmethod
    def _save_map(_map, _name):
        return {"map_name": "frozen"}

    def _current_mapping_initial_pose(self):
        if self.use_live_pose:
            return {"x": 2.0, "y": 2.0, "yaw_degrees": 0.0}
        return None

    async def _estimate_initial_pose_with_retry(self, selected, **kwargs):
        if self.estimate_error:
            raise NavigationControlError(self.estimate_error, "Choose map position")
        return {"x": 2.0, "y": 2.0, "yaw_degrees": 20.0}, (0, 0, 0, 0)

    async def switch_mode(self, request, *, user):
        self.calls.append((request, user))
        return {
            "navigation_mode": "DRIVING",
            "active_map": {"map_name": request["map_name"]},
            "localization_ready": True, "nav2_ready": True,
        }


def test_automatic_saved_map_estimates_then_applies_once():
    async def run():
        nav = Navigation()
        api = LocalizationSessionApi(FastAPI(), nav)
        response = await api.start({
            "source": "existing", "map_name": "saved",
            "map_revision": "frozen-revision",
        }, user="operator")
        assert response["phase"] == "QUEUED"
        await api._task
        assert api.phase == "READY"
        assert len(nav.calls) == 1
        assert nav.calls[0][0]["source"] == "existing"
        assert nav.calls[0][0]["initial_pose"]["x"] == 2
        assert api.snapshot()["localization_ready"] is True
    asyncio.run(run())


def test_current_slam_uses_frozen_saved_map_without_resaving():
    async def run():
        nav = Navigation()
        nav.use_live_pose = True
        api = LocalizationSessionApi(FastAPI(), nav)
        await api.start({
            "source": "current", "map_revision": "live-revision",
        }, user="operator")
        await api._task
        assert api.phase == "READY"
        assert api.map_name == "frozen"
        assert api.live_source_revision == "live-revision"
        assert nav.calls[0][0]["source"] == "existing"
        assert nav.calls[0][0]["map_revision"] == "frozen-revision"
    asyncio.run(run())


def test_uncertain_estimate_falls_back_to_map_click_not_origin():
    async def run():
        nav = Navigation()
        nav.estimate_error = "AWAITING_MAP_CLICK"
        api = LocalizationSessionApi(FastAPI(), nav)
        started = await api.start({
            "source": "existing", "map_name": "saved",
        }, user="operator")
        await api._task
        assert api.phase == "WAITING_FOR_CLICK"
        assert nav.calls == []
        nav.estimate_error = None
        await api.pose({
            "session_id": started["session_id"],
            "initial_pose": {"x": 2.0, "y": 2.0, "yaw_degrees": 45},
        })
        await api._task
        assert api.phase == "READY"
        assert len(nav.calls) == 1
    asyncio.run(run())


def test_unknown_requires_confirmation_before_amcl_activation():
    async def run():
        nav = Navigation()
        nav.estimate_error = "AWAITING_MAP_CLICK"
        api = LocalizationSessionApi(FastAPI(), nav)
        started = await api.start({
            "source": "existing", "map_name": "saved",
        }, user="operator")
        await api._task
        chosen = {"x": 0.5, "y": 2.0, "yaw_degrees": 0.0}
        await api.pose({
            "session_id": started["session_id"], "initial_pose": chosen,
        })
        await api._task
        assert api.phase == "POSE_CONFIRMATION_REQUIRED"
        assert nav.calls == []
        await api.pose({
            "session_id": started["session_id"],
            "initial_pose": {**chosen, "warning_acknowledged": True},
        })
        await api._task
        assert api.phase == "READY"
        assert len(nav.calls) == 1
    asyncio.run(run())


def test_cancel_does_not_allow_overlapping_new_localization():
    async def run():
        nav = Navigation()
        blocked = asyncio.Event()

        async def estimate(*_args, **_kwargs):
            await blocked.wait()
            return {"x": 2.0, "y": 2.0, "yaw_degrees": 0.0}, (0, 0, 0, 0)

        nav._estimate_initial_pose_with_retry = estimate
        api = LocalizationSessionApi(FastAPI(), nav)
        started = await api.start({
            "source": "existing", "map_name": "saved",
        }, user="operator")
        await asyncio.sleep(0)
        await api.cancel({"session_id": started["session_id"]})
        with pytest.raises(NavigationControlError, match="Previous localization"):
            await api.start({"source": "existing", "map_name": "saved"}, user="operator")
        blocked.set()
        await api._task
        assert api.phase == "CANCELLED"
        assert nav.calls == []
    asyncio.run(run())


def test_session_id_blocks_stale_operator_pose():
    async def run():
        api = LocalizationSessionApi(FastAPI(), Navigation())
        await api.start({"source": "existing", "map_name": "saved"}, user="operator")
        with pytest.raises(NavigationControlError) as raised:
            await api.pose({
                "session_id": "another-run",
                "initial_pose": {"x": 2, "y": 2, "yaw_degrees": 0},
            })
        assert raised.value.error_code == "LOCALIZATION_SESSION_CHANGED"
        await api._task
    asyncio.run(run())
