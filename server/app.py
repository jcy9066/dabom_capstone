import asyncio
from importlib.util import module_from_spec, spec_from_file_location
import gc
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import ClientDisconnect
from starlette.middleware.sessions import SessionMiddleware

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
PERCEPTION_DIR = ROOT_DIR / "perception"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(PERCEPTION_DIR) not in sys.path:
    sys.path.insert(0, str(PERCEPTION_DIR))

from perception.device import resolve_cuda_device
from perception.frame_processor import FrameProcessor
from perception.pipeline_factory import PIPELINE_OPTIONS, create_pipeline
from perception.utils.event_taxonomy import vision_alert_type, vision_event_type
from perception.utils.telegram_notifier import TelegramNotifier
try:
    from server.lidar_ros_bridge import LidarRosBridge
    from server.encoder_ros_bridge import EncoderRosBridge
except ModuleNotFoundError as bridge_import_error:
    LidarRosBridge = None
    EncoderRosBridge = None
    logging.getLogger(__name__).warning(
        "ROS bridge dependencies are unavailable: %s",
        bridge_import_error,
    )
from server.auth_service import AuthError, AuthService
from server.database import (
    Database,
    DatabaseConfigurationError,
    DatabaseOperationError,
)
from server.env_config import env_bool, env_float, env_int, env_text
from server.logging_service import ACTION_TYPES, EVENT_TYPES, EventLogWorker, SystemStatusWriter
from server.media_service import (
    FrozenFrameCache,
    FrozenFrameTokenError,
    PrivateImageStore,
    UnsafeMediaPathError,
)
from server.navigation_control_api import NavigationControlApi
from server.navigation_map_api import NavigationMapApi
from server.privacy import PrivacyProcessingError, PrivacyProcessor
from server.navigation_process_control import NavigationProcessControl
from server.navigation_trajectory import NavigationTrajectoryTracker
from navigation.dry_run_planner import DryRunPlannerConfig, plan_scan, validate_scan_payload

load_dotenv(ENV_PATH)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = FastAPI(title="AI Patrol Robot Integrated Server")
SESSION_SECRET_KEY = env_text("SESSION_SECRET_KEY")
COOKIE_SECURE = env_bool("COOKIE_SECURE")
EMAIL_CHALLENGE_COOKIE = "dabom_email_challenge"
EMAIL_VERIFIED_COOKIE = "dabom_verified_email"
EMAIL_COOKIE_PATH = "/api/auth"
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="dabom_session",
    same_site="lax",
    https_only=COOKIE_SECURE,
    max_age=60 * 60 * 8,
)

STATIC_DIR = ROOT_DIR / "frontend" / "services" / "static"
COMPONENTS_DIR = ROOT_DIR / "frontend" / "components"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/components", StaticFiles(directory=COMPONENTS_DIR), name="components")
templates = Jinja2Templates(directory=ROOT_DIR / "frontend" / "templates")

_auth_service = None
_auth_service_lock = threading.Lock()


def get_auth_service():
    global _auth_service
    with _auth_service_lock:
        if _auth_service is None:
            _auth_service = AuthService()
        return _auth_service


def issue_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def csrf_failure(request: Request):
    expected = request.session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token", "")
    if expected and supplied and secrets.compare_digest(expected, supplied):
        return None
    return JSONResponse(
        {
            "ok": False,
            "detail": "보안 검증에 실패했습니다. 페이지를 새로고침한 뒤 다시 시도해 주세요.",
        },
        status_code=403,
    )


def auth_client_key(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


async def auth_payload(request: Request):
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JSONResponse(
            {"ok": False, "detail": "JSON 요청 본문이 필요합니다."},
            status_code=400,
        )
    if not isinstance(payload, dict):
        return None, JSONResponse(
            {"ok": False, "detail": "JSON 객체가 필요합니다."},
            status_code=400,
        )
    return payload, None


async def call_auth(method_name: str, *args):
    try:
        method = getattr(get_auth_service(), method_name)
        return await asyncio.to_thread(method, *args), None
    except AuthError as exc:
        return None, JSONResponse(
            {"ok": False, "detail": exc.message},
            status_code=exc.status_code,
        )


@app.get("/")
async def auth_index(request: Request):
    return RedirectResponse(
        url="/main" if request.session.get("user") else "/login",
        status_code=302,
    )


@app.get("/login")
async def auth_login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/main", status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": issue_csrf_token(request)},
    )


@app.get("/main")
async def auth_main_page(request: Request):
    if not request.session.get("user"):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/auth/email/send")
async def send_signup_email(request: Request):
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    result, error = await call_auth(
        "send_email_verification",
        payload.get("email"),
        auth_client_key(request),
    )
    if error:
        return error
    challenge_token = result.pop("challenge_token")
    response = JSONResponse({"ok": True, **result})
    response.set_cookie(
        EMAIL_CHALLENGE_COOKIE,
        challenge_token,
        max_age=result["expires_in_sec"],
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path=EMAIL_COOKIE_PATH,
    )
    return response


@app.post("/api/auth/email/verify")
async def verify_signup_email(request: Request):
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    verified_token, error = await call_auth(
        "verify_email_code",
        payload.get("email"),
        payload.get("code"),
        request.cookies.get(EMAIL_CHALLENGE_COOKIE),
    )
    if error:
        return error
    response = JSONResponse({"ok": True, "message": "이메일 인증이 완료되었습니다."})
    response.delete_cookie(EMAIL_CHALLENGE_COOKIE, path=EMAIL_COOKIE_PATH)
    response.set_cookie(
        EMAIL_VERIFIED_COOKIE,
        verified_token,
        max_age=get_auth_service().settings.verification_ttl_sec,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        path=EMAIL_COOKIE_PATH,
    )
    return response


@app.post("/api/auth/availability")
async def check_signup_availability(request: Request):
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    result, error = await call_auth(
        "check_availability",
        payload.get("field"),
        payload.get("value"),
    )
    if error:
        return error
    return {"ok": True, **result}


@app.post("/api/auth/register")
async def register_user(request: Request):
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    _, error = await call_auth(
        "register",
        payload,
        request.cookies.get(EMAIL_VERIFIED_COOKIE),
    )
    if error:
        return error
    response = JSONResponse(
        {
            "ok": True,
            "message": "회원가입이 완료되었습니다. 로그인해 주세요.",
        }
    )
    response.delete_cookie(EMAIL_VERIFIED_COOKIE, path=EMAIL_COOKIE_PATH)
    return response


@app.post("/api/auth/login")
async def authenticate_user(request: Request):
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    user, error = await call_auth(
        "login",
        payload.get("login_id"),
        payload.get("password"),
        auth_client_key(request),
    )
    if error:
        return error
    request.session.clear()
    request.session["user"] = user
    issue_csrf_token(request)
    return {"ok": True, "redirect_url": "/main"}


@app.post("/api/auth/logout")
async def logout_user(request: Request):
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    request.session.clear()
    issue_csrf_token(request)
    return {"ok": True, "redirect_url": "/login"}


SERVER_ROBOT_ID = env_text("ROBOT_ID")
ROBOT_CONTROL_TOKEN = env_text("ROBOT_CONTROL_TOKEN")


def robot_ingest_failure(request: Request):
    supplied_token = request.headers.get("X-Robot-Control-Token", "")
    if ROBOT_CONTROL_TOKEN and secrets.compare_digest(supplied_token, ROBOT_CONTROL_TOKEN):
        return None
    return JSONResponse(
        {"ok": False, "error": "robot authentication required"},
        status_code=401,
    )


def robot_id_failure(payload):
    robot_id = str(payload.get("robot_id", SERVER_ROBOT_ID)).strip()
    if robot_id == SERVER_ROBOT_ID:
        return None
    return JSONResponse(
        {"ok": False, "error": "robot_id is not authorized"},
        status_code=403,
    )


@app.get("/api/auth/csrf")
async def get_auth_csrf_token(request: Request):
    return {"ok": True, "csrf_token": issue_csrf_token(request)}


SERVER_HOST = env_text("SERVER_HOST")
SERVER_PORT = env_int("SERVER_PORT", minimum=1, maximum=65535)
PIPELINE = env_text("PIPELINE")
MODEL_REQUIRED = env_bool("MODEL_REQUIRED")
SAVE_RECEIVED_FRAMES = env_bool("SAVE_RECEIVED_FRAMES", default=False)
SYSTEM_STATUS_INTERVAL_SEC = env_float("SYSTEM_STATUS_INTERVAL_SEC", minimum=0.1)
EVENT_SAVE_COOLDOWN_SEC = env_float("EVENT_SAVE_COOLDOWN_SEC", minimum=0.0)
STREAM_WIDTH = env_int("STREAM_WIDTH", minimum=1)
STREAM_HEIGHT = env_int("STREAM_HEIGHT", minimum=1)
STREAM_FPS = env_float("STREAM_FPS", minimum=0.1)
STREAM_JPEG_QUALITY = env_int("STREAM_JPEG_QUALITY", minimum=1, maximum=100)
PREVIEW_MAX_FPS = env_float("PREVIEW_MAX_FPS", minimum=1.0)
INFERENCE_ENABLED = env_bool("INFERENCE_ENABLED")
VISUALIZATION_ENABLED = env_bool("VISUALIZATION_ENABLED")
MODEL_ACTIVE = INFERENCE_ENABLED or VISUALIZATION_ENABLED
STREAM_INFER_EVERY_N = env_int("STREAM_INFER_EVERY_N", minimum=1)
INFERENCE_MAX_FPS = env_float("INFERENCE_MAX_FPS", minimum=0.1)
ADAPTIVE_BATCHING_ENABLED = env_bool("ADAPTIVE_BATCHING_ENABLED")
ADAPTIVE_BATCH_MAX_WAIT_MS = env_float("ADAPTIVE_BATCH_MAX_WAIT_MS", minimum=0.0)
ACTION_DISPLAY_TTL_SEC = env_float("ACTION_DISPLAY_TTL_SEC", minimum=0.1)
TRIGGER_SUSPICIOUS_VISUAL_ENABLED = env_bool("TRIGGER_SUSPICIOUS_VISUAL_ENABLED")
INFERENCE_MAX_RESULT_AGE_SEC = env_float("INFERENCE_MAX_RESULT_AGE_SEC", minimum=0.0)
INFERENCE_DROP_OLDER_THAN_SEC = env_float("INFERENCE_DROP_OLDER_THAN_SEC", minimum=0.0)
CUDA_DEVICE_INDEX = env_text("CUDA_DEVICE_INDEX")
DEVICE = env_text("DEVICE").lower()
GPU_REQUIRED_FOR_INFERENCE = env_bool("GPU_REQUIRED_FOR_INFERENCE")
ENV_RELOAD_CHECK_INTERVAL_SEC = env_float("ENV_RELOAD_CHECK_INTERVAL_SEC", minimum=0.1)
CAMERA_TIMEOUT_SEC = env_float("CAMERA_TIMEOUT_SEC", minimum=0.1)
ROBOT_STATUS_TIMEOUT_SEC = env_float("ROBOT_STATUS_TIMEOUT_SEC", minimum=0.1)
SAVE_DIR = ROOT_DIR / "received_frames"
NAVIGATION_MAP_DIR = ROOT_DIR / "navigation" / "maps"
NAVIGATION_MAP_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_TOKEN = env_text("TELEGRAM_TOKEN", allow_empty=True)
TELEGRAM_CHAT_ID = env_text("TELEGRAM_CHAT_ID", allow_empty=True)

state_lock = threading.Lock()
frame_condition = threading.Condition(state_lock)
server_started_at = time.time()
current_frame = None
current_frame_seq = 0
active_stream_id = 0
latest_result = {
    "ok": True,
    "robot_id": SERVER_ROBOT_ID,
    "detections": [],
    "danger": False,
    "pipeline": None,
    "model_error": None,
}
robot_status = {
    "robot_id": SERVER_ROBOT_ID,
    "cpu_usage": None,
    "cpu_temp": None,
    "ram_usage": None,
    "internet": "unknown",
    "mode": "manual",
    "updated_at": None,
}
encoder_state = {
    "robot_id": SERVER_ROBOT_ID,
    "sequence": 0,
    "left_front_ticks": 0,
    "right_front_ticks": 0,
    "left_rear_ticks": 0,
    "right_rear_ticks": 0,
    "pico_timestamp_ms": 0,
    "pi_timestamp": None,
    "updated_at": None,
}

NAVIGATION_TIMEOUT_SEC = env_float("NAVIGATION_TIMEOUT_SEC", minimum=0.0)
VALID_NAVIGATION_MODES = frozenset(
    {"scan_only", "mapping", "localization", "localization_nav2"}
)
NAVIGATION_MODE_LABELS = {
    "scan_only": "Scan only",
    "mapping": "Mapping",
    "localization": "Localization",
    "localization_nav2": "Localization + Nav2",
}
NAV_DRY_RUN_CONFIG = DryRunPlannerConfig(
    enabled=env_bool("NAV_DRY_RUN_ENABLED"),
    stop_distance_m=env_float("NAV_STOP_DISTANCE_M", minimum=0.0),
    slow_distance_m=env_float("NAV_SLOW_DISTANCE_M", minimum=0.0),
    normal_linear_mps=env_float("NAV_NORMAL_LINEAR_MPS", minimum=0.0),
    slow_linear_mps=env_float("NAV_SLOW_LINEAR_MPS", minimum=0.0),
    turn_angular_rps=env_float("NAV_TURN_ANGULAR_RPS", minimum=0.0),
    scan_timeout_sec=env_float("NAV_SCAN_TIMEOUT_SEC", minimum=0.0),
)
# Missing or empty remains fail-safe false; invalid values fail startup.
MOTOR_OUTPUT_ENABLED = env_bool("MOTOR_OUTPUT_ENABLED", default=False)
MAX_WHEEL_MPS = env_float("MAX_WHEEL_MPS", minimum=0.01)
navigation_state = {
    "robot_id": SERVER_ROBOT_ID,
    "mode": None,
    "mode_updated_at": None,
    "map": None,
    "pose": None,
    "scan": None,
    "decision": None,
    "map_updated_at": None,
    "map_revision": None,
    "pose_updated_at": None,
    "scan_updated_at": None,
}
navigation_trajectory = NavigationTrajectoryTracker()
frame_stats = {"last_time": time.time(), "count": 0, "fps": 0}
decode_stats = {"last_time": time.time(), "count": 0, "fps": 0}
publish_stats = {"last_time": time.time(), "count": 0, "fps": 0, "last_publish_at": None}
inference_rate_stats = {"last_time": time.time(), "count": 0, "fps": 0}
inference_stats = {
    "requested": 0,
    "completed": 0,
    "dropped": 0,
    "last_ms": None,
    "last_detector_ms": None,
    "last_trigger_ms": None,
    "last_action_ms": None,
    "last_render_ms": None,
    "last_adaptive_wait_ms": None,
    "last_person_count": 0,
    "last_detection_count": 0,
    "last_result_at": None,
    "last_input_seq": None,
    "last_started_at": None,
    "device": DEVICE,
}
stream_stats = {
    "connected": False,
    "robot_id": None,
    "connected_at": None,
    "disconnected_at": None,
    "bytes_received": 0,
    "frames_received": 0,
    "last_byte_at": None,
    "frames_decoded": 0,
    "frames_inferred": 0,
    "last_frame_at": None,
    "last_error": None,
    "ffmpeg_returncode": None,
    "ffmpeg_stderr_tail": [],
}
status_frame_cache = {"key": None, "frame": None}
frame_processor = None
model_error = None
model_reload_lock = threading.Lock()
database = Database()
automatic_notifier = TelegramNotifier()
event_log_worker = None
system_status_writer = None
privacy_processor = None
privacy_processor_lock = threading.Lock()
frozen_frame_cache = FrozenFrameCache()
private_image_store = None
private_image_store_lock = threading.Lock()


def robot_status_snapshot():
    with state_lock:
        return dict(robot_status)


def start_persistence_workers():
    global event_log_worker, system_status_writer
    if event_log_worker is None:
        event_log_worker = EventLogWorker(
            database,
            automatic_notifier,
            cooldown_sec=EVENT_SAVE_COOLDOWN_SEC,
            image_store=get_private_image_store(),
        )
    if system_status_writer is None:
        system_status_writer = SystemStatusWriter(
            database,
            robot_status_snapshot,
            interval_sec=SYSTEM_STATUS_INTERVAL_SEC,
        )
    event_log_worker.start()
    system_status_writer.start()


def stop_persistence_workers():
    global event_log_worker, system_status_writer
    if system_status_writer is not None:
        system_status_writer.stop()
        system_status_writer = None
    if event_log_worker is not None:
        event_log_worker.stop()
        event_log_worker = None


def submit_automatic_event(
    event_source,
    event_type,
    confidence=None,
    message=None,
    robot_id=SERVER_ROBOT_ID,
    frame=None,
):
    worker = event_log_worker
    if worker is None:
        return False
    event_frame = frame
    if event_source == "VISION_AI" and event_frame is None:
        with state_lock:
            encoded_frame = bytes(current_frame) if current_frame is not None else None
        if encoded_frame is not None:
            try:
                event_frame = decode_camera_frame(encoded_frame)
            except ValueError:
                event_frame = None
    return worker.submit(
        robot_id=robot_id,
        event_source=event_source,
        event_type=event_type,
        confidence=confidence,
        message=message,
        location=robot_status_snapshot(),
        frame=event_frame,
    )


def get_privacy_processor():
    global privacy_processor
    with privacy_processor_lock:
        if privacy_processor is None:
            privacy_processor = PrivacyProcessor()
        return privacy_processor


def get_private_image_store():
    global private_image_store
    with private_image_store_lock:
        if private_image_store is None:
            private_image_store = PrivateImageStore(
                project_root=ROOT_DIR,
                processor=get_privacy_processor(),
            )
        return private_image_store


def decode_camera_frame(encoded_frame):
    encoded = np.frombuffer(encoded_frame, dtype=np.uint8)
    frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if frame is None or frame.size == 0:
        raise ValueError("Current camera frame could not be decoded.")
    return frame


env_reload_state = {
    "mtime_ns": None,
    "signature": None,
    "last_check_at": 0.0,
    "last_loaded_at": None,
    "last_error": None,
}
processing_lock = threading.Lock()
inference_condition = threading.Condition()
inference_slot = {
    "frame": None,
    "robot_id": None,
    "frame_seq": None,
    "captured_at": None,
    "stream_id": None,
}
inference_worker_thread = None


class RobotConnectionManager:
    def __init__(self):
        self.active = {}
        self.lock = asyncio.Lock()
        self.pending_acks = {}
        self.ack_timeout_sec = env_float(
            "ROBOT_COMMAND_ACK_TIMEOUT_SEC",
            minimum=0.1,
        )

    async def connect(self, robot_id, websocket):
        await websocket.accept()
        async with self.lock:
            old = self.active.get(robot_id)
            self.active[robot_id] = websocket
            pending = []
            for command_id, (pending_robot_id, future) in list(self.pending_acks.items()):
                if pending_robot_id == robot_id:
                    pending.append(future)
                    self.pending_acks.pop(command_id, None)
        for future in pending:
            if not future.done():
                future.set_result({"ok": False, "error": "robot connection replaced"})
        if old is not None:
            try:
                await old.close()
            except Exception:
                pass

    async def disconnect(self, robot_id, websocket):
        disconnected = False
        async with self.lock:
            if self.active.get(robot_id) is websocket:
                self.active.pop(robot_id, None)
                disconnected = True
                pending = [
                    future
                    for pending_robot_id, future in self.pending_acks.values()
                    if pending_robot_id == robot_id
                ]
            else:
                pending = []
        for future in pending:
            if not future.done():
                future.set_result({"ok": False, "error": "robot disconnected"})
        return disconnected

    async def send_command(self, robot_id, command):
        async with self.lock:
            websocket = self.active.get(robot_id)
        if websocket is None:
            return False
        await websocket.send_json(command)
        return True

    async def send_command_wait_ack(self, robot_id, command):
        command = dict(command)
        command_id = str(command.get("command_id") or uuid4())
        command["command_id"] = command_id
        command.setdefault("issued_at", time.time())
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self.lock:
            websocket = self.active.get(robot_id)
            if websocket is None:
                return False
            self.pending_acks[command_id] = (robot_id, future)
        try:
            await websocket.send_json(command)
            ack = await asyncio.wait_for(future, timeout=self.ack_timeout_sec)
            return isinstance(ack, dict) and ack.get("ok") is True
        except Exception:
            return False
        finally:
            async with self.lock:
                self.pending_acks.pop(command_id, None)

    async def receive_ack(self, robot_id, message):
        command_id = str(message.get("command_id") or "")
        if not command_id:
            return False
        async with self.lock:
            pending = self.pending_acks.get(command_id)
        if pending is None or pending[0] != robot_id:
            return False
        future = pending[1]
        if not future.done():
            future.set_result(dict(message))
        return True

    async def is_connected(self, robot_id):
        async with self.lock:
            return robot_id in self.active


connections = RobotConnectionManager()

# Load the dashboard-only routes without importing frontend.__init__.
_system_control_spec = spec_from_file_location(
    "dabom_system_control",
    ROOT_DIR / "frontend" / "system_control.py",
)
if _system_control_spec is None or _system_control_spec.loader is None:
    raise RuntimeError("Unable to load system control routes.")
_system_control_module = module_from_spec(_system_control_spec)
_system_control_spec.loader.exec_module(_system_control_module)
navigation_process_control = NavigationProcessControl(ROOT_DIR)
_system_control_module.attach_system_control_routes(
    app,
    navigation_process_control=navigation_process_control,
)

navigation_map_api = NavigationMapApi(
    app=app,
    root_dir=ROOT_DIR,
    map_dir=NAVIGATION_MAP_DIR,
    csrf_failure=csrf_failure,
)


def current_navigation_map_snapshot():
    with state_lock:
        current = navigation_state.get("map")
        return dict(current) if isinstance(current, dict) else None


navigation_control_api = NavigationControlApi(
    app=app,
    map_api=navigation_map_api,
    process_control=navigation_process_control,
    csrf_failure=csrf_failure,
    robot_id=SERVER_ROBOT_ID,
    get_live_map=current_navigation_map_snapshot,
    save_map=lambda payload, name: save_navigation_map_files(payload, name),
    send_robot_command=connections.send_command_wait_ack,
    on_mode_changed=navigation_trajectory.reset,
    motor_output_enabled=MOTOR_OUTPUT_ENABLED,
)

lidar_ros_bridge = None
encoder_ros_bridge = None


def cuda_status():
    status = {
        "requested_device": DEVICE,
        "resolved_device": None,
        "cuda_device_index": CUDA_DEVICE_INDEX,
        "available": False,
        "gpu_name": None,
        "gpu_memory_used_mb": None,
        "error": None,
    }
    try:
        import torch

        resolved_device = resolve_cuda_device(DEVICE)
        index = int(resolved_device.split(":", 1)[1])
        status["resolved_device"] = resolved_device
        status["available"] = True
        status["gpu_name"] = torch.cuda.get_device_name(index)
        status["gpu_memory_used_mb"] = round(
            torch.cuda.memory_allocated(index) / (1024 * 1024), 1
        )
    except Exception as exc:
        status["error"] = str(exc)
    return status


RUNTIME_MODEL_ENV_KEYS = (
    "PIPELINE",
    "MODEL_REQUIRED",
    "INFERENCE_ENABLED",
    "VISUALIZATION_ENABLED",
    "CUDA_DEVICE_INDEX",
    "DEVICE",
    "GPU_REQUIRED_FOR_INFERENCE",
    "STREAM_INFER_EVERY_N",
    "INFERENCE_MAX_FPS",
    "ADAPTIVE_BATCHING_ENABLED",
    "ADAPTIVE_BATCH_MAX_WAIT_MS",
    "ACTION_DISPLAY_TTL_SEC",
    "TRIGGER_SUSPICIOUS_VISUAL_ENABLED",
    "INFERENCE_MAX_RESULT_AGE_SEC",
    "INFERENCE_DROP_OLDER_THAN_SEC",
)


def read_runtime_model_config():
    cuda_device_index = env_text("CUDA_DEVICE_INDEX")
    device = env_text("DEVICE").lower()
    inference_enabled = env_bool("INFERENCE_ENABLED")
    visualization_enabled = env_bool("VISUALIZATION_ENABLED")
    return {
        "pipeline": env_text("PIPELINE"),
        "model_required": env_bool("MODEL_REQUIRED"),
        "inference_enabled": inference_enabled,
        "visualization_enabled": visualization_enabled,
        "model_active": inference_enabled or visualization_enabled,
        "stream_infer_every_n": env_int(
            "STREAM_INFER_EVERY_N", minimum=1
        ),
        "inference_max_fps": env_float(
            "INFERENCE_MAX_FPS", minimum=0.1
        ),
        "adaptive_batching_enabled": env_bool("ADAPTIVE_BATCHING_ENABLED"),
        "adaptive_batch_max_wait_ms": env_float(
            "ADAPTIVE_BATCH_MAX_WAIT_MS", minimum=0.0
        ),
        "action_display_ttl_sec": env_float(
            "ACTION_DISPLAY_TTL_SEC", minimum=0.1
        ),
        "trigger_suspicious_visual_enabled": env_bool(
            "TRIGGER_SUSPICIOUS_VISUAL_ENABLED"
        ),
        "inference_max_result_age_sec": env_float(
            "INFERENCE_MAX_RESULT_AGE_SEC", minimum=0.0
        ),
        "inference_drop_older_than_sec": env_float(
            "INFERENCE_DROP_OLDER_THAN_SEC", minimum=0.0
        ),
        "cuda_device_index": cuda_device_index,
        "device": device,
        "gpu_required_for_inference": env_bool("GPU_REQUIRED_FOR_INFERENCE"),
    }


def apply_runtime_model_config(config):
    global PIPELINE, MODEL_REQUIRED, INFERENCE_ENABLED, VISUALIZATION_ENABLED, MODEL_ACTIVE
    global STREAM_INFER_EVERY_N, INFERENCE_MAX_FPS, ADAPTIVE_BATCHING_ENABLED
    global ADAPTIVE_BATCH_MAX_WAIT_MS, ACTION_DISPLAY_TTL_SEC, TRIGGER_SUSPICIOUS_VISUAL_ENABLED
    global INFERENCE_MAX_RESULT_AGE_SEC, INFERENCE_DROP_OLDER_THAN_SEC
    global CUDA_DEVICE_INDEX, DEVICE, GPU_REQUIRED_FOR_INFERENCE

    PIPELINE = config["pipeline"]
    MODEL_REQUIRED = config["model_required"]
    INFERENCE_ENABLED = config["inference_enabled"]
    VISUALIZATION_ENABLED = config["visualization_enabled"]
    MODEL_ACTIVE = config["model_active"]
    STREAM_INFER_EVERY_N = config["stream_infer_every_n"]
    INFERENCE_MAX_FPS = config["inference_max_fps"]
    ADAPTIVE_BATCHING_ENABLED = config["adaptive_batching_enabled"]
    ADAPTIVE_BATCH_MAX_WAIT_MS = config["adaptive_batch_max_wait_ms"]
    ACTION_DISPLAY_TTL_SEC = config["action_display_ttl_sec"]
    TRIGGER_SUSPICIOUS_VISUAL_ENABLED = config[
        "trigger_suspicious_visual_enabled"
    ]
    INFERENCE_MAX_RESULT_AGE_SEC = config["inference_max_result_age_sec"]
    INFERENCE_DROP_OLDER_THAN_SEC = config["inference_drop_older_than_sec"]
    CUDA_DEVICE_INDEX = config["cuda_device_index"]
    DEVICE = config["device"]
    GPU_REQUIRED_FOR_INFERENCE = config["gpu_required_for_inference"]
    with state_lock:
        inference_stats["device"] = DEVICE


def runtime_model_signature(config):
    return config["pipeline"], config["model_active"], config["device"]


def detach_frame_processor():
    global frame_processor
    with processing_lock:
        processor = frame_processor
        frame_processor = None
    return processor


def release_model_processor(processor):
    if processor is None:
        return
    try:
        del processor
        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception as exc:
        print(f"[model] cleanup warning: {exc}")


def set_model_state(pipeline_name=None, error=None):
    global model_error
    model_error = error
    with state_lock:
        latest_result["pipeline"] = pipeline_name
        latest_result["model_error"] = error
        inference_stats["device"] = DEVICE


def reload_model_pipeline(config, reason="env", initial=False):
    pipeline_name = PIPELINE_OPTIONS.get(str(config["pipeline"]))

    if not config["model_active"]:
        old_processor = detach_frame_processor()
        release_model_processor(old_processor)
        set_model_state(pipeline_name=pipeline_name, error=None)
        print("[model] inference and visualization disabled")
        return True

    if pipeline_name is None:
        old_processor = detach_frame_processor()
        release_model_processor(old_processor)
        error = f"unsupported pipeline: {config['pipeline']}"
        set_model_state(error=error)
        print(f"[model] pipeline load failed: {error}")
        if initial and config["model_required"]:
            raise RuntimeError(error)
        return False

    gpu = cuda_status()
    if not gpu["available"]:
        old_processor = detach_frame_processor()
        release_model_processor(old_processor)
        error = gpu["error"] or f"GPU device unavailable: {config['device']}"
        set_model_state(pipeline_name=pipeline_name, error=error)
        print(f"[model] GPU check failed: {error}")
        if initial and config["model_required"]:
            raise RuntimeError(error)
        return False

    old_processor = detach_frame_processor()
    release_model_processor(old_processor)
    try:
        pipeline = create_pipeline(config["pipeline"], device=config["device"])
        new_processor = FrameProcessor(
            detector=pipeline["detector"],
            action_analyzer=pipeline["action_analyzer"],
        )
        with processing_lock:
            global frame_processor
            frame_processor = new_processor
        set_model_state(pipeline_name=pipeline["name"], error=None)
        start_inference_worker()
        env_reload_state["last_loaded_at"] = time.time()
        print(
            f"[model] pipeline loaded: {pipeline['name']} "
            f"device={config['device']} reason={reason}"
        )
        return True
    except Exception as exc:
        error = str(exc)
        set_model_state(pipeline_name=pipeline_name, error=error)
        print(f"[model] pipeline load failed: {error}")
        if initial and config["model_required"]:
            raise
        return False


def ensure_runtime_model_config(force=False, reason="env"):
    now = time.time()
    if (
        not force
        and now - env_reload_state["last_check_at"]
        < ENV_RELOAD_CHECK_INTERVAL_SEC
    ):
        return

    with model_reload_lock:
        now = time.time()
        if (
            not force
            and now - env_reload_state["last_check_at"]
            < ENV_RELOAD_CHECK_INTERVAL_SEC
        ):
            return
        env_reload_state["last_check_at"] = now

        try:
            mtime_ns = ENV_PATH.stat().st_mtime_ns
        except FileNotFoundError:
            mtime_ns = None

        if not force and mtime_ns == env_reload_state["mtime_ns"]:
            return

        for key in RUNTIME_MODEL_ENV_KEYS:
            os.environ.pop(key, None)
        load_dotenv(ENV_PATH, override=True)
        try:
            config = read_runtime_model_config()
        except Exception as exc:
            error = f"runtime env parse failed: {exc}"
            env_reload_state["mtime_ns"] = mtime_ns
            env_reload_state["last_error"] = error
            set_model_state(
                pipeline_name=latest_result.get("pipeline"),
                error=error,
            )
            print(f"[model] {error}")
            return

        apply_runtime_model_config(config)
        signature = runtime_model_signature(config)
        env_reload_state["mtime_ns"] = mtime_ns

        if force or signature != env_reload_state["signature"]:
            env_reload_state["signature"] = signature
            success = reload_model_pipeline(
                config,
                reason=reason,
                initial=force,
            )
            env_reload_state["last_error"] = None if success else model_error
        else:
            env_reload_state["last_error"] = None


def encode_jpeg(frame):
    ok, buffer = cv2.imencode(
        ".jpg",
        frame,
        [int(cv2.IMWRITE_JPEG_QUALITY), STREAM_JPEG_QUALITY],
    )
    if not ok:
        raise RuntimeError("encode failed")
    return buffer.tobytes()


def update_rate_counter(counter, now):
    counter["count"] += 1
    if now - counter["last_time"] >= 1.0:
        counter["fps"] = counter["count"]
        counter["count"] = 0
        counter["last_time"] = now


def build_empty_result(robot_id):
    return {
        "ok": True,
        "robot_id": robot_id,
        "detections": [],
        "danger": False,
        "pipeline": latest_result.get("pipeline"),
        "model_error": model_error,
    }


def inference_result_is_fresh(now=None):
    now = now or time.time()
    with state_lock:
        last_result_at = inference_stats.get("last_result_at")
    return (
        last_result_at is not None
        and now - last_result_at <= INFERENCE_MAX_RESULT_AGE_SEC
    )


def clamp_box(box, width, height):
    x1, y1, x2, y2 = map(int, box)
    x1 = max(0, min(width - 1, x1))
    y1 = max(0, min(height - 1, y1))
    x2 = max(0, min(width - 1, x2))
    y2 = max(0, min(height - 1, y2))
    return x1, y1, x2, y2


def draw_overlay_label(frame, text, origin, color):
    if not text:
        return
    x, y = origin
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.55
    thickness = 2
    (text_w, text_h), baseline = cv2.getTextSize(text, font, scale, thickness)
    y = max(text_h + 8, y)
    cv2.rectangle(
        frame,
        (x, y - text_h - baseline - 6),
        (x + text_w + 8, y + baseline),
        color,
        -1,
    )
    cv2.putText(
        frame,
        text,
        (x + 4, y - 4),
        font,
        scale,
        (255, 255, 255),
        thickness,
        cv2.LINE_AA,
    )


PERSON_BASE_COLOR = (120, 220, 120)
PERSON_BASE_OPACITY = 0.30


def draw_translucent_box(frame, pt1, pt2, color, opacity=PERSON_BASE_OPACITY):
    overlay = frame.copy()
    cv2.rectangle(overlay, pt1, pt2, color, -1)
    cv2.rectangle(overlay, pt1, pt2, color, 2)
    cv2.addWeighted(overlay, opacity, frame, 1 - opacity, 0, frame)


DEFAULT_SKELETON_LINKS = [
    (15, 13),
    (13, 11),
    (16, 14),
    (14, 12),
    (11, 12),
    (5, 11),
    (6, 12),
    (5, 6),
    (5, 7),
    (6, 8),
    (7, 9),
    (8, 10),
    (1, 2),
    (0, 1),
    (0, 2),
    (1, 3),
    (2, 4),
]


def skeleton_to_list(skeleton):
    if skeleton is None:
        return None
    return [[float(x), float(y)] for x, y in skeleton]


def draw_skeleton_points(frame, skeleton, color):
    if not skeleton:
        return
    points = [(int(x), int(y)) for x, y in skeleton]
    height, width = frame.shape[:2]
    for x, y in points:
        if 0 <= x < width and 0 <= y < height:
            cv2.circle(frame, (x, y), 2, color, -1)

    links = getattr(
        getattr(frame_processor, "action_analyzer", None),
        "skeleton_links",
        DEFAULT_SKELETON_LINKS,
    )
    for start, end in links:
        if start >= len(points) or end >= len(points):
            continue
        x1, y1 = points[start]
        x2, y2 = points[end]
        if (x1, y1) == (0, 0) or (x2, y2) == (0, 0):
            continue
        if (
            0 <= x1 < width
            and 0 <= y1 < height
            and 0 <= x2 < width
            and 0 <= y2 < height
        ):
            cv2.line(frame, (x1, y1), (x2, y2), color, 1)


def draw_detection_overlay(frame, detection):
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = clamp_box(detection["box"], width, height)
    is_person = detection.get("cls", 0) == 0
    is_danger = bool(detection.get("danger"))
    has_action_label = bool(detection.get("label"))
    confidence_level = detection.get("confidence_level")
    is_suspicious = bool(
        confidence_level == "suspicious"
        or (
            TRIGGER_SUSPICIOUS_VISUAL_ENABLED
            and confidence_level == "trigger_suspicious"
        )
    )
    color = (
        (0, 0, 255)
        if (is_danger or not is_person)
        else (
            (0, 165, 255)
            if (has_action_label or is_suspicious)
            else PERSON_BASE_COLOR
        )
    )
    label = detection.get("label") or ("" if is_person else "WEAPON")
    score = detection.get("score")
    if score is not None and detection.get("label"):
        label = f"{label} {float(score) * 100:.0f}%"

    # Bounding box visualization disabled. Keep this block for easy rollback.
    # if is_person and not has_action_label and not is_danger:
    #     draw_translucent_box(frame, (x1, y1), (x2, y2), color)
    # else:
    #     cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
    draw_skeleton_points(frame, detection.get("skeleton"), color)
    draw_overlay_label(frame, label, (x1, y1 - 8), color)


def render_latest_overlay(frame, now=None):
    if not inference_result_is_fresh(now):
        return frame
    with state_lock:
        result = dict(latest_result)
    detections = result.get("detections") or []
    if not detections:
        return frame

    display_frame = frame.copy()
    for detection in detections:
        if detection.get("box"):
            draw_detection_overlay(display_frame, detection)
    return display_frame


def build_preview_frame(frame, infer, now=None):
    if infer and frame_processor is not None:
        return render_latest_overlay(frame, now)
    return frame


def collect_action_results(frame, tracked_boxes):
    persons = [obj for obj in tracked_boxes if obj.get("cls", 0) == 0]
    if not persons:
        return {}

    analyzer = frame_processor.action_analyzer
    if hasattr(analyzer, "process_many"):
        return analyzer.process_many(frame, persons)

    return {obj["id"]: analyzer.process(frame, obj) for obj in persons}


def select_action_result(model_action, heuristic_action):
    if model_action and heuristic_action:
        if bool(heuristic_action.get("is_danger")) and not bool(
            model_action.get("is_danger")
        ):
            return heuristic_action
        if bool(model_action.get("is_danger")) and not bool(
            heuristic_action.get("is_danger")
        ):
            return model_action
        return (
            model_action
            if float(model_action.get("score", 0.0))
            >= float(heuristic_action.get("score", 0.0))
            else heuristic_action
        )
    return model_action or heuristic_action


def process_frame_for_dashboard(frame):
    if frame_processor is None:
        return {
            "frame": frame,
            "detections": [],
            "danger": False,
            "timings": {},
        }

    detector_started = time.time()
    tracked_boxes = frame_processor.detector.track(frame)
    detector_ms = (time.time() - detector_started) * 1000

    trigger_started = time.time()
    obj_states = frame_processor.trigger.get_object_states(tracked_boxes)
    trigger_ms = (time.time() - trigger_started) * 1000

    action_started = time.time()
    action_results = collect_action_results(frame, tracked_boxes)
    skeletons_by_id = {
        oid: result[0]
        for oid, result in action_results.items()
        if result and result[0] is not None
    }
    violence_results = frame_processor.violence_heuristic.update(
        tracked_boxes,
        skeletons_by_id,
    )
    action_ms = (time.time() - action_started) * 1000

    render_started = time.time()
    display_frame = frame.copy()
    detections = []
    danger = False
    height, width = display_frame.shape[:2]

    for obj in tracked_boxes:
        oid = obj["id"]
        cls_id = obj.get("cls", 0)
        state = obj_states.get(oid, 0)
        x1, y1, x2, y2 = clamp_box(obj["box"], width, height)
        color = PERSON_BASE_COLOR if cls_id == 0 else (0, 0, 255)
        label = "" if cls_id == 0 else "WEAPON"
        skeleton = None

        detection = {
            "id": oid,
            "cls": cls_id,
            "box": [float(v) for v in obj["box"]],
            "state": state,
            "label": "",
            "score": None,
            "danger": False,
            "has_skeleton": False,
            "skeleton": None,
            "confidence_level": None,
            "visual_state": "normal",
        }

        if cls_id == 0:
            skeleton, action = action_results.get(oid, (None, None))
            detection["has_skeleton"] = skeleton is not None
            detection["skeleton"] = skeleton_to_list(skeleton)

            now = time.time()
            heuristic_action = violence_results.get(oid)
            selected_action = select_action_result(action, heuristic_action)
            if selected_action:
                selected_action = dict(selected_action)
                selected_action["updated_at"] = now
                frame_processor.action_display_buffer[oid] = selected_action

            current_action = frame_processor.action_display_buffer.get(oid)
            if (
                current_action
                and now - current_action.get("updated_at", 0.0)
                > ACTION_DISPLAY_TTL_SEC
            ):
                frame_processor.action_display_buffer.pop(oid, None)
                current_action = None

            if current_action:
                detection["label"] = current_action["label"]
                detection["score"] = float(current_action["score"])
                detection["danger"] = bool(current_action["is_danger"])
                detection["confidence_level"] = current_action.get(
                    "confidence_level"
                )
                if current_action["is_danger"]:
                    danger = True
                    color = (0, 0, 255)
                    detection["visual_state"] = "danger"
                    label = (
                        f"!!! {current_action['label']} !!! "
                        f"{current_action['score'] * 100:.0f}%"
                    )
                    event_type = vision_event_type(current_action["label"])
                    if event_type:
                        submit_automatic_event(
                            "VISION_AI",
                            event_type,
                            confidence=detection["score"],
                            message=f"위험 행동 감지: {current_action['label']}",
                            frame=frame,
                        )
                    else:
                        automatic_notifier.send_event_alert_async(
                            f"위험 행동 감지: {current_action['label']}",
                            robot_id=SERVER_ROBOT_ID,
                            event_type=vision_alert_type(current_action["label"]),
                        )
                else:
                    color = (0, 165, 255)
                    detection["visual_state"] = "suspicious"
                    label = (
                        f"[{current_action['label']}] "
                        f"{current_action['score'] * 100:.0f}%"
                    )
            elif TRIGGER_SUSPICIOUS_VISUAL_ENABLED and state == 1:
                color = (0, 165, 255)
                detection["confidence_level"] = "trigger_suspicious"
                detection["visual_state"] = "suspicious"
        else:
            danger = True
            detection["label"] = "WEAPON"
            detection["danger"] = True

        # Bounding box visualization disabled. Keep this block for easy rollback.
        # if cls_id == 0 and not detection["label"] and not detection["danger"]:
        #     draw_translucent_box(display_frame, (x1, y1), (x2, y2), color)
        # else:
        #     cv2.rectangle(display_frame, (x1, y1), (x2, y2), color, 2)
        if skeleton is not None:
            frame_processor.action_analyzer.draw_skeleton(
                display_frame,
                skeleton,
                color,
            )
        draw_overlay_label(display_frame, label, (x1, y1 - 8), color)
        detections.append(detection)

    render_ms = (time.time() - render_started) * 1000
    return {
        "frame": display_frame,
        "detections": detections,
        "danger": danger,
        "timings": {
            "detector_ms": round(detector_ms, 1),
            "trigger_ms": round(trigger_ms, 1),
            "action_ms": round(action_ms, 1),
            "render_ms": round(render_ms, 1),
            "person_count": sum(
                1 for obj in tracked_boxes if obj.get("cls", 0) == 0
            ),
            "detection_count": len(tracked_boxes),
        },
    }


def publish_preview_frame(frame, robot_id=SERVER_ROBOT_ID, original_bytes=None):
    global current_frame, current_frame_seq

    frame_bytes = encode_jpeg(frame)
    with frame_condition:
        now = time.time()
        current_frame = frame_bytes
        current_frame_seq += 1
        stream_stats["connected"] = True
        stream_stats["robot_id"] = robot_id
        stream_stats["frames_received"] = stream_stats.get("frames_received", 0) + 1
        stream_stats["last_frame_at"] = now
        stream_stats["last_error"] = None
        update_rate_counter(frame_stats, now)
        update_rate_counter(publish_stats, now)
        publish_stats["last_publish_at"] = now
        frame_condition.notify_all()
        frame_seq = current_frame_seq

    if SAVE_RECEIVED_FRAMES and original_bytes is not None:
        try:
            get_privacy_processor().save_image(SAVE_DIR / "latest.jpg", frame)
        except PrivacyProcessingError as exc:
            logging.getLogger(__name__).error(
                "Received frame was not saved because privacy processing failed: %s",
                exc,
            )

    return frame_seq


def submit_inference_frame(frame, robot_id, frame_seq, captured_at, stream_id=None):
    with inference_condition:
        if inference_slot["frame"] is not None:
            inference_stats["dropped"] += 1
        inference_slot.update(
            {
                "frame": frame,
                "robot_id": robot_id,
                "frame_seq": frame_seq,
                "captured_at": captured_at,
                "stream_id": stream_id,
            }
        )
        inference_stats["requested"] += 1
        inference_condition.notify()


def update_latest_result(result, frame_seq=None, input_captured_at=None, elapsed_ms=None):
    global latest_result

    now = time.time()
    result["processed_at"] = now
    if frame_seq is not None:
        result["source_frame_seq"] = frame_seq
    if input_captured_at is not None:
        result["input_age_ms"] = round((now - input_captured_at) * 1000, 1)
    if elapsed_ms is not None:
        result["inference_ms"] = round(elapsed_ms, 1)

    timings = result.get("timings") or {}
    with state_lock:
        latest_result = result
        inference_stats["completed"] += 1
        inference_stats["last_ms"] = result.get("inference_ms")
        inference_stats["last_detector_ms"] = timings.get("detector_ms")
        inference_stats["last_trigger_ms"] = timings.get("trigger_ms")
        inference_stats["last_action_ms"] = timings.get("action_ms")
        inference_stats["last_render_ms"] = timings.get("render_ms")
        inference_stats["last_adaptive_wait_ms"] = timings.get("adaptive_wait_ms")
        inference_stats["last_person_count"] = timings.get("person_count", 0)
        inference_stats["last_detection_count"] = timings.get("detection_count", 0)
        inference_stats["last_result_at"] = now
        inference_stats["last_input_seq"] = frame_seq
        update_rate_counter(inference_rate_stats, now)
        stream_stats["frames_inferred"] = stream_stats.get("frames_inferred", 0) + 1


def inference_worker():
    while True:
        min_interval = 1.0 / INFERENCE_MAX_FPS
        adaptive_wait_sec = ADAPTIVE_BATCH_MAX_WAIT_MS / 1000.0
        with inference_condition:
            while inference_slot["frame"] is None:
                inference_condition.wait()

            now = time.time()
            last_started_at = inference_stats.get("last_started_at")
            if last_started_at is not None:
                wait_sec = min_interval - (now - last_started_at)
                if wait_sec > 0:
                    inference_condition.wait(timeout=wait_sec)
                    continue

            adaptive_wait_ms = 0.0
            if ADAPTIVE_BATCHING_ENABLED and adaptive_wait_sec > 0:
                adaptive_started = time.time()
                inference_condition.wait(timeout=adaptive_wait_sec)
                now = time.time()
                adaptive_wait_ms = (now - adaptive_started) * 1000

            frame = inference_slot["frame"]
            robot_id = inference_slot["robot_id"]
            frame_seq = inference_slot["frame_seq"]
            captured_at = inference_slot["captured_at"]
            stream_id = inference_slot["stream_id"]
            inference_slot.update(
                {
                    "frame": None,
                    "robot_id": None,
                    "frame_seq": None,
                    "captured_at": None,
                    "stream_id": None,
                }
            )
            inference_stats["last_started_at"] = now

        if stream_id is not None:
            with state_lock:
                is_stale_stream = stream_id != active_stream_id
            if is_stale_stream:
                inference_stats["dropped"] += 1
                continue

        if (
            captured_at is not None
            and time.time() - captured_at > INFERENCE_DROP_OLDER_THAN_SEC
        ):
            inference_stats["dropped"] += 1
            continue

        if frame_processor is None:
            continue

        started = time.time()
        try:
            with processing_lock:
                processed = process_frame_for_dashboard(frame)
            if stream_id is not None:
                with state_lock:
                    is_stale_stream = stream_id != active_stream_id
                if is_stale_stream:
                    inference_stats["dropped"] += 1
                    continue

            display_frame_seq = publish_preview_frame(
                processed["frame"],
                robot_id=robot_id,
            )
            result = build_empty_result(robot_id)
            result["detections"] = processed["detections"]
            result["danger"] = processed["danger"]
            result["timings"] = dict(processed.get("timings") or {})
            result["timings"]["adaptive_wait_ms"] = round(adaptive_wait_ms, 1)
            elapsed_ms = (time.time() - started) * 1000
            update_latest_result(
                result,
                frame_seq=display_frame_seq,
                input_captured_at=captured_at,
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            with state_lock:
                stream_stats["last_error"] = str(exc)
                latest_result["model_error"] = str(exc)
            print(f"[inference] worker error: {exc}")
            submit_automatic_event(
                "SYSTEM_MONITOR",
                "SYSTEM_ERROR",
                message="AI inference worker error detected.",
            )


def start_inference_worker():
    global inference_worker_thread
    if inference_worker_thread is not None:
        return
    inference_worker_thread = threading.Thread(
        target=inference_worker,
        daemon=True,
    )
    inference_worker_thread.start()


@app.on_event("startup")
async def startup():
    global lidar_ros_bridge, encoder_ros_bridge

    start_persistence_workers()
    ensure_runtime_model_config(force=True, reason="startup")

    lidar_enabled = env_bool("LIDAR_ENABLE")

    if lidar_enabled and LidarRosBridge is not None and lidar_ros_bridge is None:
        lidar_ros_bridge = LidarRosBridge(
            ros_topic=env_text("LIDAR_ROS_TOPIC"),
            base_frame=env_text("LIDAR_BASE_FRAME"),
            lidar_frame=env_text("LIDAR_FRAME"),
            lidar_x=env_float("LIDAR_X"),
            lidar_y=env_float("LIDAR_Y"),
            lidar_z=env_float("LIDAR_Z"),
            lidar_yaw=env_float("LIDAR_YAW"),
            dashboard_max_points=env_int(
                "LIDAR_DASHBOARD_MAX_POINTS", minimum=1
            ),
            use_source_timestamp=env_bool("LIDAR_USE_SOURCE_TIMESTAMP"),
        )
        lidar_ros_bridge.start()

    encoder_enabled = env_bool("ENCODER_ROS_ENABLE")

    if (
        encoder_enabled
        and EncoderRosBridge is not None
        and encoder_ros_bridge is None
    ):
        encoder_ros_bridge = EncoderRosBridge(ros_topic=env_text("ENCODER_ROS_TOPIC"))
        encoder_ros_bridge.start()

    if not navigation_map_api.start():
        logging.getLogger(__name__).warning(
            "Navigation ROS control is unavailable; map loading will remain disabled."
        )
    await navigation_control_api.start()


@app.on_event("shutdown")
async def shutdown_lidar_bridge():
    global lidar_ros_bridge, encoder_ros_bridge
    try :
        await navigation_control_api.close()
    finally :
        await asyncio.to_thread(stop_persistence_workers)
    navigation_map_api.close()
    await asyncio.to_thread(navigation_process_control.stop)

    if encoder_ros_bridge is not None:
        encoder_ros_bridge.close()
        encoder_ros_bridge = None

    if lidar_ros_bridge is not None:
        lidar_ros_bridge.close()
        lidar_ros_bridge = None


def authenticated_user(request):
    user = request.session.get("user")
    if not isinstance(user, dict) or not user.get("user_id"):
        return None, JSONResponse(
            {"ok": False, "detail": "Authentication required."},
            status_code=401,
        )
    return user, None


def frozen_frame_owner(request, user):
    session_id = request.session.get("csrf_token") or issue_csrf_token(request)
    return int(user["user_id"]), str(session_id)


def encode_private_preview(frame):
    private_frame = get_privacy_processor().process(frame)
    ok, encoded = cv2.imencode(".jpg", private_frame)
    if not ok:
        raise PrivacyProcessingError("Private preview encoding failed.")
    return encoded.tobytes()


def resolve_available_media(image_path, category):
    if not image_path:
        return None
    image_store = get_private_image_store()
    try:
        target = image_store.resolve(image_path)
        target.relative_to((image_store.gallery_root / category).resolve())
        return target if target.is_file() else None
    except (UnsafeMediaPathError, ValueError, OSError):
        return None


def public_log_rows(rows, method_name):
    public_rows = []
    for row in rows:
        public = dict(row)
        image_path = public.pop("image_path", None)
        public.pop("video_path", None)
        public.pop("lidar_z", None)
        if method_name == "list_events":
            event_id = public.get("event_id")
            has_image = resolve_available_media(image_path, "events") is not None
            public["has_image"] = has_image
            public["image_url"] = (
                f"/api/media/events/{event_id}" if has_image and event_id is not None else None
            )
        elif method_name == "list_actions":
            action_id = public.get("action_id")
            has_image = resolve_available_media(image_path, "actions") is not None
            public["has_image"] = has_image
            public["image_url"] = (
                f"/api/media/actions/{action_id}"
                if has_image and action_id is not None
                else None
            )
        public_rows.append(public)
    return public_rows


def gallery_dto(row):
    source = str(row.get("source") or "").strip().lower()
    if source not in {"event", "action"}:
        return None
    source_type = source.upper()
    source_id = row.get("record_id")
    if source == "event":
        source_id = row.get("event_id", source_id)
        image_url = f"/api/media/events/{source_id}"
        category = "events"
    else:
        source_id = row.get("action_id", source_id)
        image_url = f"/api/media/actions/{source_id}"
        category = "actions"
    if source_id is None or resolve_available_media(row.get("image_path"), category) is None:
        return None
    public = {
        key: value
        for key, value in dict(row).items()
        if key not in {"image_path", "record_id"}
    }
    public.update(
        {
            "source": source,
            "source_type": source_type,
            "source_id": source_id,
            "has_image": True,
            "image_url": image_url,
            "created_at": row.get("recorded_at"),
        }
    )
    return public


LOG_PAGE_SIZE = 50
LOG_SORT_FIELDS = {
    "list_system_status": {
        "recorded_at",
        "cpu_usage",
        "cpu_temperature",
        "ram_usage",
        "ping",
        "speed",
    },
    "list_events": {
        "detected_at",
        "event_type",
        "confidence",
        "is_resolved",
        "is_reported",
        "is_alerted",
        "is_false_alarm",
    },
    "list_actions": {"created_at", "user_name", "action_type", "event_id"},
}
LOG_DEFAULT_SORT_FIELDS = {
    "list_system_status": "recorded_at",
    "list_events": "detected_at",
    "list_actions": "created_at",
}


def parse_log_filters(request, method_name=None):
    def parse_time(name):
        raw = request.query_params.get(name)
        if not raw:
            return None
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    if method_name is None:
        try:
            limit = int(request.query_params.get("limit", "100"))
            start_at = parse_time("start_at")
            end_at = parse_time("end_at")
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid time range or limit.") from exc
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")
        if start_at is not None and end_at is not None and start_at > end_at:
            raise ValueError("start_at must not be after end_at.")
        return {"start_at": start_at, "end_at": end_at, "limit": limit}

    def parse_bool(name):
        raw = request.query_params.get(name)
        if raw is None or raw == "":
            return None
        normalized = raw.strip().lower()
        if normalized in {"1", "true"}:
            return True
        if normalized in {"0", "false"}:
            return False
        raise ValueError(f"{name} must be true or false.")

    def parse_confidence(name):
        raw = request.query_params.get(name)
        if raw is None or raw == "":
            return None
        value = float(raw)
        if not 0 <= value <= 1:
            raise ValueError(f"{name} must be between 0 and 1.")
        return value

    try:
        page = int(request.query_params.get("page", "1"))
        page_size = int(request.query_params.get("page_size", str(LOG_PAGE_SIZE)))
        start_at = parse_time("start_at")
        end_at = parse_time("end_at")
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid time range or pagination.") from exc
    if page < 1:
        raise ValueError("page must be at least 1.")
    if page_size != LOG_PAGE_SIZE:
        raise ValueError(f"page_size must be {LOG_PAGE_SIZE}.")
    if start_at is not None and end_at is not None and start_at > end_at:
        raise ValueError("start_at must not be after end_at.")

    sort_by = (
        request.query_params.get("sort_by")
        or LOG_DEFAULT_SORT_FIELDS[method_name]
    ).strip().lower()
    sort_direction = request.query_params.get("sort_direction", "desc").strip().lower()
    if sort_by not in LOG_SORT_FIELDS[method_name]:
        raise ValueError(f"Unsupported sort field: {sort_by}.")
    if sort_direction not in {"asc", "desc"}:
        raise ValueError("sort_direction must be asc or desc.")

    filters = {
        "start_at": start_at,
        "end_at": end_at,
        "page": page,
        "page_size": page_size,
        "sort_by": sort_by,
        "sort_direction": sort_direction,
    }
    if method_name == "list_events":
        event_type = request.query_params.get("event_type")
        if event_type:
            event_type = event_type.strip().upper()
            if event_type not in EVENT_TYPES:
                raise ValueError("Unsupported event_type.")
        try:
            confidence_min = parse_confidence("confidence_min")
            confidence_max = parse_confidence("confidence_max")
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc) or "Invalid confidence range.") from exc
        if (
            confidence_min is not None
            and confidence_max is not None
            and confidence_min > confidence_max
        ):
            raise ValueError("confidence_min must not exceed confidence_max.")
        filters.update(
            {
                "event_type": event_type,
                "confidence_min": confidence_min,
                "confidence_max": confidence_max,
                "is_resolved": parse_bool("is_resolved"),
                "is_reported": parse_bool("is_reported"),
                "is_alerted": parse_bool("is_alerted"),
                "is_false_alarm": parse_bool("is_false_alarm"),
            }
        )
    elif method_name == "list_actions":
        action_type = request.query_params.get("action_type")
        if action_type:
            action_type = action_type.strip().upper()
            if action_type not in ACTION_TYPES:
                raise ValueError("Unsupported action_type.")
        user_name = request.query_params.get("user_name")
        filters.update(
            {
                "user_name": user_name.strip() if user_name and user_name.strip() else None,
                "action_type": action_type,
            }
        )
    return filters


async def read_persisted_logs(request, method_name):
    _, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    try:
        filters = parse_log_filters(request, method_name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    try:
        method = getattr(database, method_name)
        result = await asyncio.to_thread(method, **filters)
        if isinstance(result, dict):
            items = await asyncio.to_thread(
                public_log_rows, result.get("items", []), method_name
            )
            return {
                "items": items,
                "page": int(result.get("page", filters["page"])),
                "page_size": int(result.get("page_size", LOG_PAGE_SIZE)),
                "total": int(result.get("total", len(items))),
                "total_pages": int(result.get("total_pages", 0)),
            }
        items = await asyncio.to_thread(public_log_rows, result, method_name)
        return {
            "items": items,
            "page": filters["page"],
            "page_size": LOG_PAGE_SIZE,
            "total": len(items),
            "total_pages": 1 if items else 0,
        }
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    except (DatabaseConfigurationError, DatabaseOperationError):
        return JSONResponse(
            {"ok": False, "detail": "Database is unavailable."},
            status_code=503,
        )


def parse_log_delete_selection(payload):
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")

    def parse_ids(name):
        values = payload.get(name, [])
        if not isinstance(values, list):
            raise ValueError(f"{name} must be an array.")
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError(f"{name} must contain positive integers.")
        if len(values) != len(set(values)):
            raise ValueError(f"{name} must not contain duplicate IDs.")
        return values

    selection = {
        "event_ids": parse_ids("event_ids"),
        "action_ids": parse_ids("action_ids"),
    }
    if not selection["event_ids"] and not selection["action_ids"]:
        raise ValueError("At least one event_id or action_id is required.")
    return selection


async def process_log_soft_delete(request, *, preview):
    _, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    try:
        selection = parse_log_delete_selection(payload)
        method = (
            database.preview_log_soft_delete
            if preview
            else database.soft_delete_logs
        )
        counts = await asyncio.to_thread(method, **selection)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    except (DatabaseConfigurationError, DatabaseOperationError):
        return JSONResponse(
            {"ok": False, "detail": "Database is unavailable."},
            status_code=503,
        )
    return {"ok": True, "selection": selection, "counts": counts}


@app.get("/api/logs/system-status")
async def read_system_status_logs(request: Request):
    return await read_persisted_logs(request, "list_system_status")


@app.get("/api/logs/events")
async def read_event_logs(request: Request):
    return await read_persisted_logs(request, "list_events")


@app.get("/api/logs/actions")
async def read_action_logs(request: Request):
    return await read_persisted_logs(request, "list_actions")


@app.post("/api/logs/delete/preview")
async def preview_log_soft_delete(request: Request):
    return await process_log_soft_delete(request, preview=True)


@app.post("/api/logs/delete")
async def delete_logs(request: Request):
    return await process_log_soft_delete(request, preview=False)


@app.post("/api/logs/current-situation/preview")
async def preview_current_situation(request: Request):
    user, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    with state_lock:
        if current_frame is None:
            return JSONResponse(
                {"ok": False, "detail": "Current camera frame is unavailable."},
                status_code=503,
            )
        encoded_frame = bytes(current_frame)
        frame_sequence = current_frame_seq
    try:
        frame = await asyncio.to_thread(decode_camera_frame, encoded_frame)
        preview_jpeg = await asyncio.to_thread(encode_private_preview, frame)
        user_id, session_id = frozen_frame_owner(request, user)
        frame_token = frozen_frame_cache.store(
            frame,
            user_id=user_id,
            session_id=session_id,
            frame_sequence=frame_sequence,
        )
    except (ValueError, PrivacyProcessingError):
        return JSONResponse(
            {"ok": False, "detail": "Current camera preview is unavailable."},
            status_code=503,
        )
    return Response(
        content=preview_jpeg,
        media_type="image/jpeg",
        headers={"X-Frame-Token": frame_token},
    )


@app.post("/api/logs/current-situation")
async def create_current_situation(request: Request):
    user, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    include_image = payload.get("include_image", False)
    if not isinstance(include_image, bool):
        return JSONResponse(
            {"ok": False, "detail": "include_image must be a boolean."},
            status_code=400,
        )
    description = payload.get("description_content")
    description = None if description is None else str(description).strip()
    if description and len(description) > 2000:
        return JSONResponse(
            {"ok": False, "detail": "description_content is too long."},
            status_code=400,
        )
    if not description and not include_image:
        return JSONResponse(
            {"ok": False, "detail": "Text or an image is required."},
            status_code=400,
        )

    frame_token = payload.get("frame_token")
    frozen = None
    user_id, session_id = frozen_frame_owner(request, user)
    if include_image:
        if not isinstance(frame_token, str) or not frame_token.strip():
            return JSONResponse(
                {"ok": False, "detail": "frame_token is required."},
                status_code=400,
            )
        frame_token = frame_token.strip()
        try:
            frozen = frozen_frame_cache.get(
                frame_token,
                user_id=user_id,
                session_id=session_id,
            )
        except FrozenFrameTokenError as exc:
            return JSONResponse(
                {"ok": False, "detail": str(exc)},
                status_code=400,
            )

    image_path = None
    image_store = None
    if frozen is not None:
        try:
            image_store = get_private_image_store()
            image_path = await asyncio.to_thread(
                image_store.save_image,
                "actions",
                frozen.frame,
            )
        except Exception:
            return JSONResponse(
                {"ok": False, "detail": "Private image could not be saved."},
                status_code=500,
            )
    try:
        action_id = await asyncio.to_thread(
            database.insert_action,
            user_id=user_id,
            event_id=None,
            action_type="NOTE",
            description=description or None,
            image_path=image_path,
        )
    except Exception as exc:
        if image_path is not None:
            try:
                await asyncio.to_thread(image_store.delete, image_path)
            except Exception as cleanup_exc:
                logging.getLogger(__name__).warning(
                    "Current situation image cleanup failed: %s", cleanup_exc
                )
        status_code = (
            503
            if isinstance(exc, (DatabaseConfigurationError, DatabaseOperationError))
            else 500
        )
        return JSONResponse(
            {"ok": False, "detail": "Current situation could not be saved."},
            status_code=status_code,
        )
    if frame_token is not None:
        frozen_frame_cache.invalidate(
            frame_token,
            user_id=user_id,
            session_id=session_id,
        )
    return JSONResponse(
        {
            "ok": True,
            "action_id": action_id,
            "image_url": (
                f"/api/media/actions/{action_id}" if image_path is not None else None
            ),
        },
        status_code=201,
    )


@app.get("/api/gallery")
async def read_gallery(request: Request):
    _, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    source = request.query_params.get("source", "all").strip().lower()
    if source not in {"all", "event", "action"}:
        return JSONResponse(
            {"ok": False, "detail": "source must be one of: all, event, action"},
            status_code=400,
        )
    try:
        filters = parse_log_filters(request)
        rows = await asyncio.to_thread(database.list_gallery, source=source, **filters)
    except ValueError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    except (DatabaseConfigurationError, DatabaseOperationError):
        return JSONResponse(
            {"ok": False, "detail": "Database is unavailable."},
            status_code=503,
        )
    gallery_rows = await asyncio.to_thread(
        lambda: [dto for row in rows if (dto := gallery_dto(row)) is not None]
    )
    return gallery_rows


@app.delete("/api/gallery/{source}/{record_id}")
async def delete_gallery_image(request: Request, source: str, record_id: int):
    _, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    source = source.strip().lower()
    if source not in {"event", "action"}:
        return JSONResponse(
            {"ok": False, "detail": "source must be one of: event, action"},
            status_code=400,
        )
    if record_id <= 0:
        return JSONResponse(
            {"ok": False, "detail": "record_id must be a positive integer."},
            status_code=400,
        )
    database_method = (
        database.soft_delete_event_image
        if source == "event"
        else database.soft_delete_action_image
    )
    try:
        deleted = await asyncio.to_thread(database_method, record_id)
    except (DatabaseConfigurationError, DatabaseOperationError):
        return JSONResponse(
            {"ok": False, "detail": "Database is unavailable."},
            status_code=503,
        )
    if not deleted:
        return JSONResponse({"ok": False, "detail": "Image not found."}, status_code=404)
    return {"ok": True, "source": source, "record_id": record_id}


async def authenticated_media(request, record_id, *, category, database_method):
    _, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    try:
        image_path = await asyncio.to_thread(database_method, record_id)
    except (DatabaseConfigurationError, DatabaseOperationError):
        return JSONResponse(
            {"ok": False, "detail": "Database is unavailable."},
            status_code=503,
        )
    if not image_path:
        return JSONResponse({"ok": False, "detail": "Image not found."}, status_code=404)
    target = resolve_available_media(image_path, category)
    if target is None:
        return JSONResponse({"ok": False, "detail": "Image not found."}, status_code=404)
    return FileResponse(target, media_type="image/jpeg")


@app.get("/api/media/events/{event_id}")
async def read_event_image(request: Request, event_id: int):
    return await authenticated_media(
        request,
        event_id,
        category="events",
        database_method=database.get_event_image_path,
    )


@app.get("/api/media/actions/{action_id}")
async def read_action_image(request: Request, action_id: int):
    return await authenticated_media(
        request,
        action_id,
        category="actions",
        database_method=database.get_action_image_path,
    )


@app.patch("/api/logs/events/{event_id}/false-alarm")
async def update_event_false_alarm(request: Request, event_id: int):
    _, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    if event_id <= 0:
        return JSONResponse(
            {"ok": False, "detail": "event_id must be a positive integer."},
            status_code=400,
        )
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    is_false_alarm = payload.get("is_false_alarm")
    if not isinstance(is_false_alarm, bool):
        return JSONResponse(
            {"ok": False, "detail": "is_false_alarm must be a boolean."},
            status_code=400,
        )
    try:
        updated = await asyncio.to_thread(
            database.set_event_false_alarm,
            event_id,
            is_false_alarm,
        )
        if not updated:
            exists = await asyncio.to_thread(database.event_exists, event_id)
            if not exists:
                return JSONResponse(
                    {"ok": False, "detail": "Event not found."},
                    status_code=404,
                )
    except (DatabaseConfigurationError, DatabaseOperationError):
        return JSONResponse(
            {"ok": False, "detail": "Database is unavailable."},
            status_code=503,
        )
    return {
        "ok": True,
        "event_id": event_id,
        "is_false_alarm": is_false_alarm,
    }


@app.post("/api/logs/actions")
async def create_action_log(request: Request):
    user, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    payload, payload_error = await auth_payload(request)
    if payload_error:
        return payload_error
    action_type = str(payload.get("action_type") or "").strip().upper()
    if action_type not in ACTION_TYPES:
        return JSONResponse(
            {"ok": False, "detail": "Unsupported action_type."},
            status_code=400,
        )
    event_id = payload.get("event_id")
    try:
        event_id = None if event_id in (None, "") else int(event_id)
        if event_id is not None and event_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "detail": "event_id must be a positive integer or null."},
            status_code=400,
        )
    description = payload.get("description", payload.get("description_content"))
    description = None if description is None else str(description).strip()
    if description and len(description) > 2000:
        return JSONResponse(
            {"ok": False, "detail": "description is too long."},
            status_code=400,
        )
    try:
        action_id = await asyncio.to_thread(
            database.insert_action,
            user_id=int(user["user_id"]),
            event_id=event_id,
            action_type=action_type,
            description=description or None,
        )
    except (DatabaseConfigurationError, DatabaseOperationError):
        return JSONResponse(
            {"ok": False, "detail": "Database is unavailable."},
            status_code=503,
        )
    return JSONResponse({"ok": True, "action_id": action_id}, status_code=201)


@app.post("/send_telegram")
async def send_telegram(request: Request):
    user, auth_error = authenticated_user(request)
    if auth_error:
        return auth_error
    csrf_error = csrf_failure(request)
    if csrf_error:
        return csrf_error
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return JSONResponse(
            {"status": "error", "error": "telegram env missing"},
            status_code=500,
        )

    try:
        raw_body = await request.body()
        payload = json.loads(raw_body) if raw_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"status": "error", "error": "invalid JSON body"},
            status_code=400,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"status": "error", "error": "JSON object required"},
            status_code=400,
        )
    try:
        raw_event_id = payload.get("event_id")
        event_id = None if raw_event_id in (None, "") else int(raw_event_id)
        if event_id is not None and event_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse(
            {"status": "error", "error": "invalid event_id"},
            status_code=400,
        )

    if event_id is not None:
        try:
            event_exists = await asyncio.to_thread(database.event_exists, event_id)
        except (DatabaseConfigurationError, DatabaseOperationError):
            return JSONResponse(
                {"status": "error", "error": "database unavailable"},
                status_code=503,
            )
        if not event_exists:
            return JSONResponse(
                {"status": "error", "error": "event not found"},
                status_code=404,
            )

    try:
        action_id = await asyncio.to_thread(
            database.insert_action,
            user_id=int(user["user_id"]),
            event_id=event_id,
            action_type="REPORT",
            description="Manual Telegram danger report requested.",
        )
    except (DatabaseConfigurationError, DatabaseOperationError):
        return JSONResponse(
            {"status": "error", "error": "database unavailable"},
            status_code=503,
        )

    message = "[긴급] 순찰 로봇 위험 감지! 관제 센터에서 신고가 접수되었습니다."
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=5,
        )
        if response.status_code != 200:
            return JSONResponse({"status": "error"}, status_code=502)
        persistence_warning = None
        if event_id is not None:
            try:
                marked = await asyncio.to_thread(database.mark_event_reported, event_id)
            except (DatabaseConfigurationError, DatabaseOperationError):
                marked = False
                persistence_warning = "Telegram sent, but the event update failed."
            if not marked:
                persistence_warning = persistence_warning or (
                    "Telegram sent, but the event was not updated."
                )
        result = {"status": "success", "action_id": action_id}
        if persistence_warning:
            result["persistence_warning"] = persistence_warning
        return result
    except Exception as exc:
        print(f"텔레그램 전송 오류: {exc}")
        return JSONResponse({"status": "error"}, status_code=502)


@app.post("/status")
@app.post("/update_status")
async def update_status(request: Request):
    global robot_status
    denied = robot_ingest_failure(request)
    if denied:
        return denied
    data = await request.json()
    if not isinstance(data, dict) or not data:
        return JSONResponse(
            {"ok": False, "error": "empty status"},
            status_code=400,
        )
    denied = robot_id_failure(data)
    if denied:
        return denied

    with state_lock:
        def status_value(name, current=None):
            value = data.get(name, current)
            return None if value is None else value

        robot_status.update(
            {
                "robot_id": data.get("robot_id", robot_status["robot_id"]),
                "cpu_usage": status_value("cpu_usage", robot_status["cpu_usage"]),
                "cpu_temp": status_value("cpu_temp", robot_status["cpu_temp"]),
                "ram_usage": status_value("ram_usage", robot_status["ram_usage"]),
                "internet": status_value("internet", robot_status["internet"]),
                "mode": status_value("mode", robot_status.get("mode", "manual")),
                "ping": status_value("ping", robot_status.get("ping")),
                "speed": status_value("speed", robot_status.get("speed")),
                "gps_lat": status_value("gps_lat", robot_status.get("gps_lat")),
                "gps_lng": status_value("gps_lng", robot_status.get("gps_lng")),
                "gps_alt": status_value("gps_alt", robot_status.get("gps_alt")),
                "lidar_x": status_value("lidar_x", robot_status.get("lidar_x")),
                "lidar_y": status_value("lidar_y", robot_status.get("lidar_y")),
                "lidar_z": status_value("lidar_z", robot_status.get("lidar_z")),
                "updated_at": time.time(),
            }
        )
    navigation_control_api.note_pi_status(data)
    return {"ok": True}


@app.get("/get_status")
async def get_status():
    with state_lock:
        return dict(robot_status)


def parse_navigation_mode(payload):
    if not isinstance(payload, dict):
        raise ValueError("navigation payload must be a JSON object")

    raw_mode = payload.get("navigation_mode")
    if raw_mode is None:
        return None

    mode = str(raw_mode).strip().lower()
    if mode not in VALID_NAVIGATION_MODES:
        allowed = ", ".join(sorted(VALID_NAVIGATION_MODES))
        raise ValueError(
            f"unsupported navigation_mode: {mode}. allowed: {allowed}"
        )
    return mode


def store_navigation_mode(mode, received_at):
    if mode is None:
        return
    navigation_state["mode"] = mode
    navigation_state["mode_updated_at"] = received_at


def build_navigation_status(now=None):
    now = now or time.time()
    decision = build_navigation_decision(now)
    last_update_at = navigation_state.get("scan_updated_at")
    last_update_age_sec = (
        None
        if last_update_at is None
        else max(0.0, now - last_update_at)
    )
    mode = navigation_state.get("mode")

    if last_update_at is None:
        nav_status = "offline"
    elif last_update_age_sec > NAVIGATION_TIMEOUT_SEC:
        nav_status = "stale"
    elif mode in VALID_NAVIGATION_MODES:
        nav_status = mode
    elif navigation_state.get("map") is not None:
        # Backward compatibility with older map_bridge payloads.
        nav_status = "mapping"
    elif navigation_state.get("scan") is not None:
        nav_status = "scan_only"
    else:
        nav_status = "online"

    return {
        "robot_id": navigation_state.get("robot_id", SERVER_ROBOT_ID),
        "status": nav_status,
        "mode": mode,
        "mode_label": NAVIGATION_MODE_LABELS.get(mode),
        "mode_updated_at": navigation_state.get("mode_updated_at"),
        "last_update_at": last_update_at,
        "last_update_age_sec": last_update_age_sec,
        "timeout_sec": NAVIGATION_TIMEOUT_SEC,
        "has_map": navigation_state.get("map") is not None,
        "has_pose": navigation_state.get("pose") is not None,
        "has_scan": navigation_state.get("scan") is not None,
        "map_updated_at": navigation_state.get("map_updated_at"),
        "pose_updated_at": navigation_state.get("pose_updated_at"),
        "scan_updated_at": navigation_state.get("scan_updated_at"),
        "dry_run": True,
        "motor_output_enabled": MOTOR_OUTPUT_ENABLED,
        "decision_action": decision["action"],
        "decision_reason": decision["reason"],
    }


def build_navigation_decision(now=None):
    """Refresh the display-only decision from the latest scan while locked."""
    decision = plan_scan(
        navigation_state.get("scan"),
        navigation_state.get("scan_updated_at"),
        NAV_DRY_RUN_CONFIG,
        now=now,
    )
    decision["robot_id"] = navigation_state.get("robot_id", SERVER_ROBOT_ID)
    navigation_state["decision"] = decision
    return decision


def received_payload(data, received_at):
    payload = dict(data)
    payload["received_at"] = received_at
    return payload


def build_navigation_map_revision(data):
    """Hash only map fields that affect dashboard rendering or coordinates."""
    revision_payload = {
        key: data.get(key)
        for key in (
            "frame_id",
            "resolution",
            "width",
            "height",
            "origin",
            "data_encoding",
            "data",
        )
    }
    encoded = json.dumps(
        revision_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sanitize_map_name(name=None):
    if name is None or not str(name).strip():
        name = f"map_{time.strftime('%Y%m%d_%H%M%S', time.localtime())}"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name).strip())
    name = (
        name.strip("._-")
        or f"map_{time.strftime('%Y%m%d_%H%M%S', time.localtime())}"
    )
    return name[:64]


def unique_map_name(base_name):
    candidate = base_name
    index = 2
    while (NAVIGATION_MAP_DIR / f"{candidate}.meta.json").exists():
        candidate = f"{base_name}_{index}"
        index += 1
    return candidate


def decode_map_cells(map_payload):
    width = int(map_payload.get("width") or 0)
    height = int(map_payload.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("invalid map dimensions")
    expected = width * height
    data = map_payload.get("data")
    encoding = map_payload.get("data_encoding", "raw")
    if not isinstance(data, list):
        raise ValueError("map data must be a list")

    if encoding == "rle":
        cells = []
        for run in data:
            if not isinstance(run, list) or len(run) < 2:
                raise ValueError("invalid rle map data")
            value = int(run[0])
            count = int(run[1])
            if count < 0:
                raise ValueError("invalid rle count")
            cells.extend([value] * count)
            if len(cells) > expected:
                raise ValueError("rle map data is longer than expected")
    else:
        cells = [int(value) for value in data]

    if len(cells) != expected:
        raise ValueError(
            f"map data length mismatch: expected {expected}, got {len(cells)}"
        )
    return width, height, cells


def occupancy_to_pgm_bytes(width, height, cells):
    # ROS map files store the top image row first, while OccupancyGrid data starts at origin.
    pixels = bytearray()
    for row in range(height - 1, -1, -1):
        offset = row * width
        for value in cells[offset : offset + width]:
            if value < 0:
                pixels.append(205)
            elif value >= 65:
                pixels.append(0)
            elif value <= 25:
                pixels.append(254)
            else:
                pixels.append(205)
    header = f"P5\n# AI patrol robot map\n{width} {height}\n255\n".encode(
        "ascii"
    )
    return header + bytes(pixels)


def save_navigation_map_files(map_payload, requested_name=None):
    width, height, cells = decode_map_cells(map_payload)
    base_name = unique_map_name(sanitize_map_name(requested_name))
    saved_at = time.time()
    saved_at_iso = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(saved_at),
    )
    pgm_path = NAVIGATION_MAP_DIR / f"{base_name}.pgm"
    yaml_path = NAVIGATION_MAP_DIR / f"{base_name}.yaml"
    meta_path = NAVIGATION_MAP_DIR / f"{base_name}.meta.json"
    raw_path = NAVIGATION_MAP_DIR / f"{base_name}.raw.json"

    origin = map_payload.get("origin") or {}
    origin_x = float(origin.get("x", 0.0))
    origin_y = float(origin.get("y", 0.0))
    origin_yaw = float(origin.get("yaw", 0.0))
    resolution = float(map_payload.get("resolution") or 0.05)

    pgm_path.write_bytes(occupancy_to_pgm_bytes(width, height, cells))
    yaml_path.write_text(
        "\n".join(
            [
                f"image: {pgm_path.name}",
                f"resolution: {resolution}",
                f"origin: [{origin_x}, {origin_y}, {origin_yaw}]",
                "negate: 0",
                "occupied_thresh: 0.65",
                "free_thresh: 0.196",
                "",
            ]
        ),
        encoding="utf-8",
    )
    raw_payload = dict(map_payload)
    raw_payload["saved_at"] = saved_at
    raw_payload["saved_at_iso"] = saved_at_iso
    raw_path.write_text(
        json.dumps(raw_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    meta = {
        "map_name": base_name,
        "robot_id": map_payload.get("robot_id", SERVER_ROBOT_ID),
        "frame_id": map_payload.get("frame_id", "map"),
        "source_timestamp": map_payload.get("timestamp"),
        "saved_at": saved_at,
        "saved_at_iso": saved_at_iso,
        "resolution": resolution,
        "width": width,
        "height": height,
        "origin": {"x": origin_x, "y": origin_y, "yaw": origin_yaw},
        "files": {
            "pgm": str(pgm_path.relative_to(ROOT_DIR)),
            "yaml": str(yaml_path.relative_to(ROOT_DIR)),
            "meta": str(meta_path.relative_to(ROOT_DIR)),
            "raw": str(raw_path.relative_to(ROOT_DIR)),
        },
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return meta


def list_saved_navigation_maps():
    maps = []
    for meta_path in sorted(
        NAVIGATION_MAP_DIR.glob("*.meta.json"),
        reverse=True,
    ):
        try:
            maps.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return maps


@app.post("/navigation/map")
async def update_navigation_map(request: Request):
    denied = robot_ingest_failure(request)
    if denied:
        return denied
    try:
        data = await request.json()
        if not isinstance(data, dict) or not data:
            raise ValueError("empty map payload")
        navigation_mode = parse_navigation_mode(data)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )
    denied = robot_id_failure(data)
    if denied:
        return denied

    received_at = time.time()
    map_revision = build_navigation_map_revision(data)
    with state_lock:
        navigation_state["robot_id"] = data.get(
            "robot_id",
            navigation_state["robot_id"],
        )
        store_navigation_mode(navigation_mode, received_at)
        navigation_state["map"] = received_payload(data, received_at)
        navigation_state["map_updated_at"] = received_at
        navigation_state["map_revision"] = map_revision
    navigation_trajectory.note_map(navigation_mode, map_revision)
    return {"ok": True}


@app.post("/navigation/pose")
async def update_navigation_pose(request: Request):
    denied = robot_ingest_failure(request)
    if denied:
        return denied
    try:
        data = await request.json()
        if not isinstance(data, dict) or not data:
            raise ValueError("empty pose payload")
        navigation_mode = parse_navigation_mode(data)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )
    denied = robot_id_failure(data)
    if denied:
        return denied

    received_at = time.time()
    with state_lock:
        navigation_state["robot_id"] = data.get(
            "robot_id",
            navigation_state["robot_id"],
        )
        store_navigation_mode(navigation_mode, received_at)
        navigation_state["pose"] = received_payload(data, received_at)
        navigation_state["pose_updated_at"] = received_at
    navigation_trajectory.note_pose(navigation_mode, data)
    navigation_control_api.note_navigation_sample("pose", received_at, data)
    return {"ok": True}


@app.post("/navigation/scan")
async def update_navigation_scan(request: Request):
    denied = robot_ingest_failure(request)
    if denied:
        return denied
    try:
        raw_data = await request.json()
        navigation_mode = parse_navigation_mode(raw_data)
        data = validate_scan_payload(raw_data)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=400,
        )
    denied = robot_id_failure(raw_data)
    if denied:
        return denied

    received_at = time.time()
    with state_lock:
        navigation_state["robot_id"] = data.get(
            "robot_id",
            navigation_state["robot_id"],
        )
        store_navigation_mode(navigation_mode, received_at)
        navigation_state["scan"] = received_payload(data, received_at)
        navigation_state["scan_updated_at"] = received_at
        build_navigation_decision(received_at)
    navigation_control_api.note_navigation_sample("scan", received_at)
    return {"ok": True}


@app.get("/api/navigation/status")
async def get_navigation_status():
    with state_lock:
        return build_navigation_status()


@app.get("/api/navigation/snapshot")
async def get_navigation_snapshot(map_revision: str | None = None):
    """Return one atomic visual snapshot, including map data only when changed."""
    with state_lock:
        status = build_navigation_status()
        current_map = navigation_state.get("map")
        current_revision = navigation_state.get("map_revision")
        pose = navigation_state.get("pose")
        scan = navigation_state.get("scan")

        if current_map is None:
            map_changed = map_revision is not None
        elif current_revision is None:
            # A legacy/injected map without a revision must never be treated as cached.
            map_changed = True
        else:
            map_changed = map_revision != current_revision

        snapshot = {
            "ok": True,
            "status": status,
            "map_available": current_map is not None,
            "map_changed": map_changed,
            "map_revision": current_revision,
            "pose_available": pose is not None,
            "pose": pose,
            "scan_available": scan is not None,
            "scan": scan,
            "trajectory": navigation_trajectory.snapshot(),
        }
        if map_changed:
            snapshot["map"] = current_map
        return snapshot


@app.get("/api/navigation/decision")
async def get_navigation_decision():
    with state_lock:
        return {"ok": True, **build_navigation_decision()}


@app.get("/api/navigation/map")
async def get_navigation_map():
    with state_lock:
        status = build_navigation_status()
        current_map = navigation_state.get("map")
    if current_map is None:
        return {
            "ok": True,
            "available": False,
            "status": status,
            "map": None,
            "error": "map unavailable",
        }
    return {
        "ok": True,
        "available": True,
        "status": status,
        "map": current_map,
    }


@app.post("/api/navigation/maps/save")
async def save_current_navigation_map(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    requested_name = None
    if isinstance(data, dict):
        requested_name = data.get("map_name") or data.get("name")
    with state_lock:
        current_map = navigation_state.get("map")
        status = build_navigation_status()
    if current_map is None:
        return JSONResponse(
            {
                "ok": False,
                "status": status,
                "error": "map unavailable",
            },
            status_code=404,
        )
    try:
        meta = save_navigation_map_files(
            dict(current_map),
            requested_name=requested_name,
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "status": status, "error": str(exc)},
            status_code=400,
        )
    return {"ok": True, "status": status, "map": meta}


@app.get("/api/navigation/maps")
async def get_saved_navigation_maps(request: Request):
    return await navigation_map_api.list_maps(request)


@app.get("/api/navigation/pose")
async def get_navigation_pose():
    with state_lock:
        status = build_navigation_status()
        pose = navigation_state.get("pose")
    if pose is None:
        return {
            "ok": True,
            "available": False,
            "status": status,
            "pose": None,
            "error": "pose unavailable",
        }
    return {
        "ok": True,
        "available": True,
        "status": status,
        "pose": pose,
    }


@app.get("/api/navigation/scan")
async def get_navigation_scan():
    with state_lock:
        status = build_navigation_status()
        scan = navigation_state.get("scan")
    if scan is None:
        return {
            "ok": True,
            "available": False,
            "status": status,
            "scan": None,
            "error": "scan unavailable",
        }
    return {
        "ok": True,
        "available": True,
        "status": status,
        "scan": scan,
    }


@app.get("/api/robots/{robot_id}")
async def get_robot(robot_id: str):
    with state_lock:
        status = dict(robot_status)
        result = dict(latest_result)
        navigation = build_navigation_status()
    return {
        "robot_id": robot_id,
        "connected": await connections.is_connected(robot_id),
        "status": status,
        "latest_result": result,
        "navigation": navigation,
    }


@app.get("/api/robots/{robot_id}/encoder")
async def get_robot_encoder(robot_id: str):
    with state_lock:
        data = dict(encoder_state)

    if data.get("updated_at") is None or data.get("robot_id") != robot_id:
        return JSONResponse(
            {
                "ok": False,
                "robot_id": robot_id,
                "error": "encoder unavailable",
            },
            status_code=404,
        )

    data["age_sec"] = max(0.0, time.time() - data["updated_at"])
    return {"ok": True, "encoder": data}


def process_and_publish_frame(
    frame,
    robot_id=SERVER_ROBOT_ID,
    original_bytes=None,
    infer=True,
):
    global latest_result

    result_frame = frame
    result = build_empty_result(robot_id)

    if infer and frame_processor is not None:
        started = time.time()
        with processing_lock:
            processed = process_frame_for_dashboard(frame)
        result_frame = processed["frame"]
        result["detections"] = processed["detections"]
        result["danger"] = processed["danger"]
        result["timings"] = dict(processed.get("timings") or {})
        result["inference_ms"] = round((time.time() - started) * 1000, 1)

    frame_seq = publish_preview_frame(
        result_frame,
        robot_id=robot_id,
        original_bytes=original_bytes,
    )
    result["source_frame_seq"] = frame_seq
    result["processed_at"] = time.time()
    with state_lock:
        latest_result = result
        if infer and frame_processor is not None:
            inference_stats["completed"] += 1
            inference_stats["last_ms"] = result.get("inference_ms")
            inference_stats["last_result_at"] = result["processed_at"]
            inference_stats["last_input_seq"] = frame_seq
            update_rate_counter(inference_rate_stats, result["processed_at"])
            stream_stats["frames_inferred"] = (
                stream_stats.get("frames_inferred", 0) + 1
            )

    return result


def build_camera_status(now=None):
    now = now or time.time()
    last_frame_at = stream_stats.get("last_frame_at")
    last_status_at = robot_status.get("updated_at")
    last_frame_age = (
        None if last_frame_at is None else max(0.0, now - last_frame_at)
    )
    last_status_age = (
        None if last_status_at is None else max(0.0, now - last_status_at)
    )
    has_frame = current_frame is not None
    frame_is_live = (
        last_frame_age is not None and last_frame_age <= CAMERA_TIMEOUT_SEC
    )
    status_is_live = (
        last_status_age is not None and last_status_age <= ROBOT_STATUS_TIMEOUT_SEC
    )
    startup_age = now - server_started_at
    last_error = stream_stats.get("last_error")
    disconnected_at = stream_stats.get("disconnected_at")
    stream_is_closed = (
        disconnected_at is not None
        and last_frame_at is not None
        and disconnected_at >= last_frame_at
        and not stream_stats.get("connected")
    )

    if not has_frame and startup_age < ROBOT_STATUS_TIMEOUT_SEC:
        state = "waiting"
        message = "카메라 신호 대기 중"
        connected = False
    elif stream_is_closed or not stream_stats.get("connected"):
        if status_is_live:
            state = "camera_disconnected"
            message = "카메라 연결이 끊겼습니다"
        else:
            state = "robot_disconnected"
            message = "라즈베리 파이와 연결이 끊겼습니다"
        connected = False
    elif frame_is_live:
        state = "live"
        message = "영상 수신 중"
        connected = True
    elif status_is_live:
        state = "camera_disconnected"
        message = "카메라 연결이 끊겼습니다"
        connected = False
    elif last_error:
        state = "error"
        message = "카메라 스트림 오류"
        connected = False
    elif not has_frame:
        state = "robot_disconnected"
        message = "라즈베리 파이와 연결이 끊겼습니다"
        connected = False
    else:
        state = "robot_disconnected"
        message = "라즈베리 파이와 연결이 끊겼습니다"
        connected = False

    return {
        "camera_state": state,
        "camera_connected": connected,
        "message": message,
        "last_frame_age_sec": last_frame_age,
        "last_status_age_sec": last_status_age,
        "camera_timeout_sec": CAMERA_TIMEOUT_SEC,
        "robot_status_timeout_sec": ROBOT_STATUS_TIMEOUT_SEC,
    }


def build_status_frame(message):
    key = (STREAM_WIDTH, STREAM_HEIGHT, message)
    if (
        status_frame_cache["key"] == key
        and status_frame_cache["frame"] is not None
    ):
        return status_frame_cache["frame"]

    frame = np.full((STREAM_HEIGHT, STREAM_WIDTH, 3), 209, dtype=np.uint8)
    panel_w = min(STREAM_WIDTH - 48, 430)
    panel_h = 116
    x1 = max(16, (STREAM_WIDTH - panel_w) // 2)
    y1 = max(16, (STREAM_HEIGHT - panel_h) // 2)
    x2 = x1 + panel_w
    y2 = y1 + panel_h
    cv2.rectangle(
        frame,
        (x1, y1),
        (x2, y2),
        (90, 96, 106),
        thickness=-1,
    )
    cv2.rectangle(
        frame,
        (x1, y1),
        (x2, y2),
        (156, 163, 175),
        thickness=2,
    )

    title = "CAMERA OFFLINE"
    detail = "Check Raspberry Pi / camera connection"
    title_size = cv2.getTextSize(
        title,
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        2,
    )[0]
    detail_size = cv2.getTextSize(
        detail,
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        1,
    )[0]
    cv2.putText(
        frame,
        title,
        ((STREAM_WIDTH - title_size[0]) // 2, y1 + 46),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        detail,
        ((STREAM_WIDTH - detail_size[0]) // 2, y1 + 78),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (229, 231, 235),
        1,
        cv2.LINE_AA,
    )

    ok, buffer = cv2.imencode(".jpg", frame)
    if not ok:
        return None
    status_frame_cache["key"] = key
    status_frame_cache["frame"] = buffer.tobytes()
    return status_frame_cache["frame"]


@app.post("/frame")
async def receive_frame(request: Request, file: UploadFile | None = File(None)):
    ensure_runtime_model_config(reason="frame")
    infer_param = request.query_params.get("infer")
    infer = (
        MODEL_ACTIVE
        if infer_param is None
        else infer_param.lower() in ("1", "true", "yes", "on")
    )
    if file is not None:
        data = await file.read()
    else:
        data = await request.body()

    np_arr = np.frombuffer(data, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if frame is None:
        return JSONResponse(
            {"ok": False, "error": "decode failed"},
            status_code=400,
        )

    try:
        return process_and_publish_frame(
            frame,
            robot_id=request.query_params.get("robot_id", SERVER_ROBOT_ID),
            original_bytes=data,
            infer=infer,
        )
    except RuntimeError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=500,
        )


def read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def h264_decode_loop(proc, robot_id, infer_override, stream_id):
    frame_size = STREAM_WIDTH * STREAM_HEIGHT * 3
    frame_index = 0
    last_preview_at = 0.0
    min_preview_interval = 1.0 / PREVIEW_MAX_FPS
    try:
        while True:
            ensure_runtime_model_config(reason="stream")
            infer = MODEL_ACTIVE if infer_override is None else infer_override
            with state_lock:
                stream_stats["infer"] = infer
                stream_stats["inference_available"] = frame_processor is not None
            raw_frame = read_exact(proc.stdout, frame_size)
            if raw_frame is None:
                break
            frame = np.frombuffer(raw_frame, np.uint8).reshape(
                (STREAM_HEIGHT, STREAM_WIDTH, 3)
            )
            frame_index += 1
            now = time.time()
            frame_seq = None
            if now - last_preview_at >= min_preview_interval:
                preview_frame = build_preview_frame(frame, infer, now)
                frame_seq = publish_preview_frame(
                    preview_frame,
                    robot_id=robot_id,
                )
                last_preview_at = now
            should_infer = (
                infer
                and frame_processor is not None
                and frame_index % STREAM_INFER_EVERY_N == 0
            )
            if should_infer:
                if frame_seq is None:
                    with state_lock:
                        frame_seq = current_frame_seq
                submit_inference_frame(
                    frame,
                    robot_id,
                    frame_seq,
                    now,
                    stream_id=stream_id,
                )
            with state_lock:
                stream_stats["frames_decoded"] += 1
                update_rate_counter(decode_stats, time.time())
                stream_stats["last_frame_at"] = time.time()
                stream_stats["ffmpeg_returncode"] = proc.poll()
    except Exception as exc:
        message = str(exc)
        with state_lock:
            stream_stats["last_error"] = message
        print(f"[stream/h264] decode loop error: {message}")


def ffmpeg_stderr_loop(proc):
    if proc.stderr is None:
        return
    try:
        for raw_line in iter(proc.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            with state_lock:
                stream_stats["ffmpeg_stderr_tail"].append(line)
                stream_stats["ffmpeg_stderr_tail"] = stream_stats[
                    "ffmpeg_stderr_tail"
                ][-20:]
                stream_stats["last_error"] = line
            print(f"[ffmpeg] {line}")
    except Exception as exc:
        print(f"[ffmpeg] stderr reader error: {exc}")


@app.api_route("/stream/h264", methods=["POST", "PUT"])
async def receive_h264_stream(request: Request):
    global active_stream_id

    ensure_runtime_model_config(reason="stream_connect")
    robot_id = request.query_params.get("robot_id", SERVER_ROBOT_ID)
    infer_param = request.query_params.get("infer")
    infer_override = (
        None
        if infer_param is None
        else infer_param.lower() in ("1", "true", "yes", "on")
    )
    infer = MODEL_ACTIVE if infer_override is None else infer_override
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-analyzeduration",
        "100000",
        "-probesize",
        "100000",
        "-f",
        "h264",
        "-i",
        "pipe:0",
        "-vf",
        f"scale={STREAM_WIDTH}:{STREAM_HEIGHT},fps={STREAM_FPS}",
        "-pix_fmt",
        "bgr24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "ffmpeg not found"},
            status_code=500,
        )

    with inference_condition:
        inference_slot.update(
            {
                "frame": None,
                "robot_id": None,
                "frame_seq": None,
                "captured_at": None,
                "stream_id": None,
            }
        )
    with state_lock:
        active_stream_id += 1
        stream_id = active_stream_id
        now = time.time()
        frame_stats.update({"last_time": now, "count": 0, "fps": 0})
        decode_stats.update({"last_time": now, "count": 0, "fps": 0})
        publish_stats.update(
            {
                "last_time": now,
                "count": 0,
                "fps": 0,
                "last_publish_at": None,
            }
        )
        inference_rate_stats.update({"last_time": now, "count": 0, "fps": 0})
        inference_stats.update(
            {
                "requested": 0,
                "completed": 0,
                "dropped": 0,
                "last_ms": None,
                "last_result_at": None,
                "last_input_seq": None,
                "last_started_at": None,
                "device": DEVICE,
            }
        )
        stream_stats.update(
            {
                "connected": True,
                "robot_id": robot_id,
                "connected_at": now,
                "disconnected_at": None,
                "bytes_received": 0,
                "frames_received": 0,
                "last_byte_at": None,
                "frames_decoded": 0,
                "frames_inferred": 0,
                "last_frame_at": None,
                "last_error": None,
                "ffmpeg_returncode": None,
                "ffmpeg_stderr_tail": [],
                "infer": infer,
                "inference_available": frame_processor is not None,
                "stream_id": stream_id,
            }
        )
    reader = threading.Thread(
        target=h264_decode_loop,
        args=(proc, robot_id, infer_override, stream_id),
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=ffmpeg_stderr_loop,
        args=(proc,),
        daemon=True,
    )
    reader.start()
    stderr_reader.start()
    print(f"[stream/h264] connected robot_id={robot_id} infer={infer}")

    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            with state_lock:
                stream_stats["bytes_received"] += len(chunk)
                stream_stats["last_byte_at"] = time.time()
                stream_stats["ffmpeg_returncode"] = proc.poll()
            if proc.stdin is None:
                break
            if proc.poll() is not None:
                with state_lock:
                    stream_stats["ffmpeg_returncode"] = proc.returncode
                break
            try:
                await asyncio.to_thread(proc.stdin.write, chunk)
                await asyncio.to_thread(proc.stdin.flush)
            except BrokenPipeError:
                with state_lock:
                    stream_stats["last_error"] = "ffmpeg stdin broken pipe"
                    stream_stats["ffmpeg_returncode"] = proc.poll()
                break
    except ClientDisconnect:
        with state_lock:
            stream_stats["connected"] = False
            stream_stats["disconnected_at"] = time.time()
            stream_stats["ffmpeg_returncode"] = proc.poll()
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass
        reader.join(timeout=2.0)
        stderr_reader.join(timeout=1.0)
        with state_lock:
            stream_stats["connected"] = False
            stream_stats["disconnected_at"] = time.time()
            stream_stats["ffmpeg_returncode"] = proc.poll()

    print(f"[stream/h264] disconnected robot_id={robot_id}")
    return {"ok": True, "robot_id": robot_id}


def generate_frames():
    last_sent_seq = 0
    last_status_sent_at = 0.0
    while True:
        with frame_condition:
            camera_status = build_camera_status()
            is_live = camera_status["camera_state"] == "live"
            if is_live:
                if current_frame is None or current_frame_seq == last_sent_seq:
                    frame_condition.wait(timeout=1.0)
                    continue
                frame = current_frame
                last_sent_seq = current_frame_seq
            else:
                now = time.time()
                wait_sec = 0.5 - (now - last_status_sent_at)
                if wait_sec > 0:
                    frame_condition.wait(timeout=wait_sec)
                    continue
                frame = build_status_frame(camera_status["message"])
                last_status_sent_at = now
                last_sent_seq = 0
        if frame is not None:
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
        else:
            time.sleep(0.1)


@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(
        generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/latest_result")
async def get_latest_result():
    with state_lock:
        return dict(latest_result)


@app.get("/api/stream_status")
async def get_stream_status():
    ensure_runtime_model_config(reason="status")
    gpu = cuda_status()
    with state_lock:
        now = time.time()
        status = dict(stream_stats)
        status.update(build_camera_status())
        status["has_current_frame"] = current_frame is not None
        status["received_fps"] = (
            frame_stats["fps"] if status["camera_state"] == "live" else 0
        )
        status["decode_fps"] = (
            decode_stats["fps"] if status["camera_state"] == "live" else 0
        )
        status["publish_fps"] = (
            publish_stats["fps"] if status["camera_state"] == "live" else 0
        )
        status["latest_frame_seq"] = current_frame_seq
        status["latest_frame_age_ms"] = (
            None
            if stream_stats.get("last_frame_at") is None
            else round((now - stream_stats["last_frame_at"]) * 1000, 1)
        )
        status["stream_width"] = STREAM_WIDTH
        status["stream_height"] = STREAM_HEIGHT
        status["stream_fps"] = STREAM_FPS
        status["stream_jpeg_quality"] = STREAM_JPEG_QUALITY
        status["inference_enabled"] = INFERENCE_ENABLED
        status["visualization_enabled"] = VISUALIZATION_ENABLED
        status["model_active"] = MODEL_ACTIVE
        status["inference_available"] = frame_processor is not None
        status["stream_infer_every_n"] = STREAM_INFER_EVERY_N
        status["inference_max_fps"] = INFERENCE_MAX_FPS
        status["adaptive_batching_enabled"] = ADAPTIVE_BATCHING_ENABLED
        status["adaptive_batch_max_wait_ms"] = ADAPTIVE_BATCH_MAX_WAIT_MS
        status["action_display_ttl_sec"] = ACTION_DISPLAY_TTL_SEC
        status["trigger_suspicious_visual_enabled"] = (
            TRIGGER_SUSPICIOUS_VISUAL_ENABLED
        )
        status["inference_max_result_age_sec"] = INFERENCE_MAX_RESULT_AGE_SEC
        status["inference_drop_older_than_sec"] = INFERENCE_DROP_OLDER_THAN_SEC
        status["inference_fps"] = (
            inference_rate_stats["fps"]
            if status["camera_state"] == "live"
            else 0
        )
        status["inference_requested_frames"] = inference_stats["requested"]
        status["inference_completed_frames"] = inference_stats["completed"]
        status["inference_dropped_frames"] = inference_stats["dropped"]
        status["inference_last_ms"] = inference_stats["last_ms"]
        status["inference_last_detector_ms"] = inference_stats[
            "last_detector_ms"
        ]
        status["inference_last_trigger_ms"] = inference_stats[
            "last_trigger_ms"
        ]
        status["inference_last_action_ms"] = inference_stats["last_action_ms"]
        status["inference_last_render_ms"] = inference_stats["last_render_ms"]
        status["inference_last_adaptive_wait_ms"] = inference_stats[
            "last_adaptive_wait_ms"
        ]
        status["inference_last_person_count"] = inference_stats[
            "last_person_count"
        ]
        status["inference_last_detection_count"] = inference_stats[
            "last_detection_count"
        ]
        status["inference_result_age_ms"] = (
            None
            if inference_stats["last_result_at"] is None
            else round((now - inference_stats["last_result_at"]) * 1000, 1)
        )
        status["inference_result_stale"] = (
            inference_stats["last_result_at"] is not None
            and now - inference_stats["last_result_at"]
            > INFERENCE_MAX_RESULT_AGE_SEC
        )
        status["inference_device"] = DEVICE
        status["cuda_device_index"] = CUDA_DEVICE_INDEX
        status["gpu_required_for_inference"] = GPU_REQUIRED_FOR_INFERENCE
        status["gpu_available"] = gpu["available"]
        status["gpu_name"] = gpu["gpu_name"]
        status["gpu_memory_used_mb"] = gpu["gpu_memory_used_mb"]
        status["gpu_error"] = gpu["error"]
        return status


@app.websocket("/ws/sensors/{robot_id}/lidar")
async def lidar_sensor_websocket(websocket: WebSocket, robot_id: str):
    supplied_token = websocket.headers.get("X-Robot-Control-Token", "")
    if not ROBOT_CONTROL_TOKEN or not secrets.compare_digest(
        supplied_token,
        ROBOT_CONTROL_TOKEN,
    ):
        await websocket.close(code=1008, reason="robot authentication required")
        return

    if lidar_ros_bridge is None:
        await websocket.close(code=1011)
        return

    await websocket.accept()
    lidar_ros_bridge.mark_connected(robot_id)
    print(f"[lidar-ws] connected robot_id={robot_id}")

    try:
        while True:
            raw_message = await websocket.receive_text()

            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError as exc:
                print(
                    f"[lidar-ws] invalid JSON robot_id={robot_id}: {exc}"
                )
                continue

            if not isinstance(message, dict):
                continue

            message_type = message.get("type")
            if message_type == "sensor_hello":
                message_robot_id = message.get("robot_id")
                if message_robot_id and message_robot_id != robot_id:
                    await websocket.close(code=1008)
                    return
                continue

            if message_type != "laser_scan":
                continue

            message_robot_id = message.get("robot_id")
            if message_robot_id and message_robot_id != robot_id:
                await websocket.close(code=1008)
                return

            message["robot_id"] = robot_id

            try:
                dashboard_scan = lidar_ros_bridge.submit(message)
            except (ValueError, RuntimeError) as exc:
                print(
                    f"[lidar-ws] rejected scan robot_id={robot_id}: {exc}"
                )
                continue

            received_at = time.time()
            with state_lock:
                navigation_state["robot_id"] = robot_id
                navigation_state["scan"] = received_payload(
                    dashboard_scan,
                    received_at,
                )
                navigation_state["scan_updated_at"] = received_at

            stats = lidar_ros_bridge.stats()
            if stats["received"] % 100 == 0:
                print(
                    f"[lidar-ws] robot_id={robot_id} "
                    f"received={stats['received']} "
                    f"published={stats['published']} "
                    f"dropped={stats['dropped']} "
                    f"points={stats['last_points']}"
                )

    except WebSocketDisconnect:
        print(f"[lidar-ws] disconnected robot_id={robot_id}")
    except Exception as exc:
        print(f"[lidar-ws] error robot_id={robot_id}: {exc}")
    finally:
        lidar_ros_bridge.mark_disconnected(robot_id)


@app.get("/api/lidar/bridge")
async def get_lidar_bridge_status():
    if lidar_ros_bridge is None:
        return {
            "ok": False,
            "enabled": False,
            "error": "LiDAR ROS bridge is not running",
        }

    return {
        "ok": True,
        "enabled": True,
        "stats": lidar_ros_bridge.stats(),
    }


@app.websocket("/ws/robot/{robot_id}")
async def robot_websocket(websocket: WebSocket, robot_id: str):
    supplied_token = websocket.headers.get("X-Robot-Control-Token", "")
    if not ROBOT_CONTROL_TOKEN or not secrets.compare_digest(supplied_token, ROBOT_CONTROL_TOKEN):
        await websocket.close(code=1008, reason="robot authentication required")
        return
    await connections.connect(robot_id, websocket)
    if robot_id == SERVER_ROBOT_ID:
        navigation_control_api.note_pi_connection(True)
    print(f"[ws] robot connected: {robot_id}")
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "status":
                status_data = message.get("data", {})
                if not isinstance(status_data, dict):
                    continue
                status_data = {
                    key: value
                    for key, value in status_data.items()
                    if key not in {"battery", "battery_level"}
                }
                with state_lock:
                    robot_status.update(status_data)
                    robot_status["robot_id"] = robot_id
                    robot_status["updated_at"] = time.time()
                if robot_id == SERVER_ROBOT_ID:
                    navigation_control_api.note_pi_status(status_data)
            elif message.get("type") == "encoder":
                data = message.get("data")

                if not isinstance(data, dict):
                    continue

                try:
                    received_encoder = {
                        "robot_id": robot_id,
                        "sequence": int(data.get("sequence", 0)),
                        "left_front_ticks": int(data["left_front_ticks"]),
                        "right_front_ticks": int(data["right_front_ticks"]),
                        "left_rear_ticks": int(data["left_rear_ticks"]),
                        "right_rear_ticks": int(data["right_rear_ticks"]),
                        "pico_timestamp_ms": int(data["pico_timestamp_ms"]),
                        "pi_timestamp": data.get("pi_timestamp"),
                        "updated_at": time.time(),
                    }
                except (KeyError, TypeError, ValueError):
                    continue

                with state_lock:
                    encoder_state.update(received_encoder)
                if robot_id == SERVER_ROBOT_ID:
                    navigation_control_api.note_encoder(received_encoder)

                if encoder_ros_bridge is not None:
                    try:
                        encoder_ros_bridge.submit(received_encoder)
                    except (ValueError, RuntimeError) as exc:
                        print(f"[encoder-ros] rejected: {exc}")

            elif message.get("type") == "ack":
                print(f"[ws] ack from {robot_id}: {message}")
                await connections.receive_ack(robot_id, message)
                if robot_id == SERVER_ROBOT_ID:
                    navigation_control_api.note_pi_status(message)
    except WebSocketDisconnect:
        print(f"[ws] robot disconnected: {robot_id}")
    finally:
        disconnected = await connections.disconnect(robot_id, websocket)
        if robot_id == SERVER_ROBOT_ID and disconnected:
            navigation_control_api.note_pi_connection(False)


@app.get("/api/encoder/bridge")
async def get_encoder_bridge_status():
    if encoder_ros_bridge is None:
        return {
            "ok": False,
            "enabled": False,
            "error": "Encoder ROS bridge is not running",
        }

    return {
        "ok": True,
        "enabled": True,
        "topic": encoder_ros_bridge.ros_topic,
        "stats": encoder_ros_bridge.stats(),
    }


@app.post("/api/robots/{robot_id}/command")
async def send_robot_command(
    robot_id: str,
    request: Request,
):
    robot_auth_error = robot_ingest_failure(request)
    if robot_auth_error:
        _, user_auth_error = authenticated_user(request)
        if user_auth_error:
            return user_auth_error
        csrf_error = csrf_failure(request)
        if csrf_error:
            return csrf_error

    try:
        payload = await request.json()
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return JSONResponse(
            {
                "ok": False,
                "error": "invalid JSON body",
            },
            status_code=400,
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            {
                "ok": False,
                "error": "JSON object required",
            },
            status_code=400,
        )

    command_type = str(
        payload.get("type", "move")
    ).strip()

    if not command_type:
        return JSONResponse(
            {
                "ok": False,
                "error": "command type is required",
            },
            status_code=400,
        )

    command = {
        **payload,
        "command_id": str(
            payload.get("command_id")
            or uuid4()
        ),
        "type": command_type,
        "issued_at": time.time(),
    }

    # Nav2 dry-run 명령은 서버까지만 수신하고
    # Pi WebSocket으로 전달하지 않는다.
    nav2_dry_run = (
        command.get("source")
        == "nav2_command_bridge"
        and command.get("dry_run") is True
        and command_type in {
            "auto_drive",
            "stop",
        }
    )

    if command_type == "auto_drive":
        try:
            raw_left = command.get("left_mps")
            raw_right = command.get("right_mps")

            if (
                isinstance(raw_left, bool)
                or isinstance(raw_right, bool)
            ):
                raise ValueError(
                    "boolean wheel speed"
                )

            left_mps = float(raw_left)
            right_mps = float(raw_right)

        except (
            TypeError,
            ValueError,
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "delivered": False,
                    "robot_id": robot_id,
                    "command": command,
                    "error": (
                        "left_mps and right_mps "
                        "must be finite numbers"
                    ),
                },
                status_code=400,
            )

        if (
            not np.isfinite(left_mps)
            or not np.isfinite(right_mps)
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "delivered": False,
                    "robot_id": robot_id,
                    "command": command,
                    "error": (
                        "NaN or Infinity is not allowed"
                    ),
                },
                status_code=400,
            )

        if (
            abs(left_mps) > MAX_WHEEL_MPS
            or abs(right_mps) > MAX_WHEEL_MPS
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "delivered": False,
                    "robot_id": robot_id,
                    "command": command,
                    "error": (
                        "wheel speed exceeds "
                        f"{MAX_WHEEL_MPS:.2f} m/s"
                    ),
                },
                status_code=400,
            )

        command["left_mps"] = left_mps
        command["right_mps"] = right_mps

        # 서버의 두 번째 자율주행 출력 안전장치.
        if not MOTOR_OUTPUT_ENABLED:
            return JSONResponse(
                {
                    "ok": True,
                    "accepted": True,
                    "delivered": False,
                    "blocked": True,
                    "dry_run": True,
                    "motor_output_enabled": False,
                    "robot_id": robot_id,
                    "command": command,
                    "error": None,
                },
                status_code=200,
            )

    if nav2_dry_run:
        return JSONResponse(
            {
                "ok": True,
                "accepted": True,
                "delivered": False,
                "blocked": True,
                "dry_run": True,
                "motor_output_enabled": (
                    MOTOR_OUTPUT_ENABLED
                ),
                "robot_id": robot_id,
                "command": command,
                "error": None,
            },
            status_code=200,
        )

    delivered = await connections.send_command(
        robot_id,
        command,
    )

    status_code = (
        200 if delivered else 409
    )

    return JSONResponse(
        {
            "ok": delivered,
            "delivered": delivered,
            "blocked": False,
            "robot_id": robot_id,
            "command": command,
            "error": (
                None
                if delivered
                else "robot not connected"
            ),
        },
        status_code=status_code,
    )


@app.get("/api/pipelines")
async def get_pipelines():
    ensure_runtime_model_config(reason="pipelines")
    with state_lock:
        loaded = latest_result.get("pipeline")
    return {
        "pipelines": PIPELINE_OPTIONS,
        "selected": PIPELINE,
        "loaded": loaded,
        "model_error": model_error,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server.app:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=False,
        access_log=False,
    )
