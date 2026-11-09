"""Isolated, authenticated voice-intercom MVP for one DABOM robot.

The relay forwards 16 kHz, mono, signed 16-bit LE PCM in 20 ms chunks.
Audio is never persisted. No motor/Pico/Nav2 code is touched.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect

logger = logging.getLogger(__name__)
MAX_AUDIO_FRAME = 4096
SEND_TIMEOUT = 0.3


class AudioRelay:
    """One dashboard and one Pi, with explicit listening/talking consent."""

    def __init__(self) -> None:
        self.dashboard: WebSocket | None = None
        self.robot: WebSocket | None = None
        self.listening = False
        self.talking = False
        self.speaker_volume_percent: int | None = None
        self._state_lock = asyncio.Lock()
        self._send_locks: dict[int, asyncio.Lock] = {}

    def state(self) -> dict:
        return {
            "type": "state",
            "dashboard_connected": self.dashboard is not None,
            "robot_connected": self.robot is not None,
            "listening": self.listening,
            "talking": self.talking,
            "speaker_volume_percent": self.speaker_volume_percent,
            "codec": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "frame_ms": 20,
        }

    async def send(self, socket: WebSocket | None, data: dict | bytes) -> bool:
        if socket is None:
            return False
        lock = self._send_locks.setdefault(id(socket), asyncio.Lock())
        try:
            async with lock:
                if isinstance(data, bytes):
                    await asyncio.wait_for(socket.send_bytes(data), SEND_TIMEOUT)
                else:
                    await asyncio.wait_for(socket.send_json(data), SEND_TIMEOUT)
            return True
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
            return False

    async def join(self, role: str, socket: WebSocket) -> bool:
        async with self._state_lock:
            if getattr(self, role) is not None:
                await socket.close(code=1008, reason="audio endpoint already connected")
                return False
            await socket.accept()
            setattr(self, role, socket)
            # Never resume audio implicitly after a reconnect.
            if role == "dashboard":
                self.listening = False
                self.talking = False
            else:
                # Read the actual ALSA value; never assume a hardcoded default.
                self.speaker_volume_percent = None
            other = self.robot if role == "dashboard" else self.dashboard
            current = self.state()
        await self.send(socket, current)
        if role == "robot":
            await self.send(socket, {"type": "listen", "enabled": False})
            await self.send(socket, {"type": "talk", "enabled": False})
            await self.send(socket, {"type": "get_speaker_volume"})
        await self.send(other, current)
        return True

    async def leave(self, role: str, socket: WebSocket) -> None:
        async with self._state_lock:
            if getattr(self, role) is not socket:
                return
            setattr(self, role, None)
            if role == "robot":
                self.speaker_volume_percent = None
            self._send_locks.pop(id(socket), None)
            self.listening = False
            self.talking = False
            other = self.robot if role == "dashboard" else self.dashboard
            current = self.state()
        if role == "dashboard":
            await self.send(other, {"type": "listen", "enabled": False})
            await self.send(other, {"type": "talk", "enabled": False})
        await self.send(other, current)

    async def dashboard_message(self, socket: WebSocket, message: dict) -> None:
        if self.dashboard is not socket:
            return
        binary = message.get("bytes")
        if binary is not None:
            if self.talking and self.robot is not None and 0 < len(binary) <= MAX_AUDIO_FRAME:
                await self.send(self.robot, binary)
            return
        raw = message.get("text")
        if not raw or len(raw) > 1024:
            return
        try:
            command = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(command, dict):
            return
        kind = command.get("type")
        if kind == "set_speaker_volume":
            percent = command.get("percent")
            if type(percent) is not int or not 0 <= percent <= 100:
                await self.send(socket, {"type": "error", "message": "USB 스피커 음량 범위는 0~100%입니다."})
                return
            async with self._state_lock:
                if self.dashboard is not socket:
                    return
                robot = self.robot
            if robot is None:
                await self.send(socket, {"type": "error", "message": "로봇 오디오가 연결되지 않았습니다."})
                return
            if not await self.send(robot, {"type": "set_speaker_volume", "percent": percent}):
                await self.send(socket, {"type": "error", "message": "로봇 USB 스피커 음량 명령 전달 실패"})
            return
        if kind not in {"listen", "talk"} or type(command.get("enabled")) is not bool:
            return
        enabled = command["enabled"]
        async with self._state_lock:
            if self.dashboard is not socket:
                return
            if enabled and self.robot is None:
                await self.send(socket, {"type": "error", "message": "로봇 오디오가 연결되지 않았습니다."})
                return
            if kind == "listen":
                self.listening = enabled
            else:
                self.talking = enabled
            robot = self.robot
            current = self.state()
        await self.send(robot, {"type": kind, "enabled": enabled})
        await self.send(socket, current)

    async def robot_message(self, socket: WebSocket, message: dict) -> None:
        if self.robot is not socket:
            return
        binary = message.get("bytes")
        if binary is not None:
            if self.listening and self.dashboard is not None and 0 < len(binary) <= MAX_AUDIO_FRAME:
                await self.send(self.dashboard, binary)
            return
        raw = message.get("text")
        if not raw or len(raw) > 1024:
            return
        try:
            command = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(command, dict):
            return
        if command.get("type") == "speaker_volume":
            percent = command.get("percent")
            if type(percent) is not int or not 0 <= percent <= 100:
                return
            async with self._state_lock:
                if self.robot is not socket:
                    return
                self.speaker_volume_percent = percent
                dashboard = self.dashboard
            await self.send(dashboard, {"type": "speaker_volume", "percent": percent})
        elif command.get("type") == "audio_error":
            # Keep device errors on the authenticated channel only.
            detail = command.get("message")
            if isinstance(detail, str):
                await self.send(self.dashboard, {"type": "error", "message": detail[:180]})


def register_audio_intercom(
    app: FastAPI,
    *,
    robot_id: str,
    robot_token: str,
    root_dir: Path,
) -> AudioRelay:
    """Register a separate test UI; existing dashboard controls stay unchanged."""
    hub = AudioRelay()
    router = APIRouter()

    def authenticated_dashboard(ws: WebSocket) -> bool:
        session = ws.scope.get("session") or {}
        origin = ws.headers.get("origin")
        host = ws.headers.get("host")
        if not session.get("user") or not origin or not host:
            return False
        parsed = urlsplit(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == host.lower()

    @router.get("/api/audio/status")
    async def status(request: Request):
        if not request.session.get("user"):
            return JSONResponse({"ok": False, "error": "login required"}, status_code=401)
        return {"ok": True, "robot_id": robot_id, **hub.state()}

    @router.get("/audio/intercom-test", response_class=HTMLResponse)
    async def test_page(request: Request):
        if not request.session.get("user"):
            return HTMLResponse("로그인이 필요합니다.", status_code=401)
        path = Path(root_dir) / "frontend/templates/audio_intercom_test.html"
        markup = path.read_text(encoding="utf-8")
        return HTMLResponse(markup.replace("__DABOM_ROBOT_ID__", html.escape(robot_id, quote=True)))

    @router.websocket("/ws/audio/dashboard/{target_robot_id}")
    async def dashboard_ws(ws: WebSocket, target_robot_id: str):
        if target_robot_id != robot_id or not authenticated_dashboard(ws):
            await ws.close(code=1008, reason="dashboard authentication required")
            return
        if not await hub.join("dashboard", ws):
            return
        try:
            while True:
                incoming = await ws.receive()
                if incoming["type"] == "websocket.disconnect":
                    break
                await hub.dashboard_message(ws, incoming)
        except WebSocketDisconnect:
            pass
        finally:
            await hub.leave("dashboard", ws)

    @router.websocket("/ws/audio/robot/{target_robot_id}")
    async def robot_ws(ws: WebSocket, target_robot_id: str):
        supplied = ws.headers.get("X-Robot-Control-Token", "")
        if (target_robot_id != robot_id or not robot_token
                or not secrets.compare_digest(supplied, robot_token)):
            await ws.close(code=1008, reason="robot authentication required")
            return
        if not await hub.join("robot", ws):
            return
        try:
            while True:
                incoming = await ws.receive()
                if incoming["type"] == "websocket.disconnect":
                    break
                await hub.robot_message(ws, incoming)
        except WebSocketDisconnect:
            pass
        finally:
            await hub.leave("robot", ws)

    app.include_router(router)
    return hub
