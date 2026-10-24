import importlib
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import cv2
import numpy as np
import pytest

from server.database import DatabaseConfigurationError, DatabaseOperationError
from server.media_service import FrozenFrameCache, FrozenFrameTokenError, UnsafeMediaPathError


os.environ["INFERENCE_ENABLED"] = "false"
os.environ["VISUALIZATION_ENABLED"] = "false"
os.environ["MODEL_REQUIRED"] = "false"
os.environ["COOKIE_SECURE"] = "false"


class FakeDatabase:
    def __init__(self):
        self.filters = None
        self.actions = []
        self.reported = []
        self.insert_error = None
        self.gallery_filters = None
        self.gallery_rows = []
        self.event_images = {}
        self.action_images = {}
        self.deleted_event_images = set()
        self.deleted_action_images = set()
        self.soft_delete_error = None
        self.log_soft_delete_error = None
        self.log_soft_delete_preview = {"events": 2, "actions": 5, "images": 4}
        self.log_soft_delete_result = {"events": 2, "actions": 5, "images": 4}
        self.log_soft_delete_calls = []
        self.false_alarm_error = None
        self.false_alarm_events = {7: False}
        self.rows = {
            "system": [],
            "events": [{"event_id": 7, "detected_at": datetime(2026, 8, 14, 9)}],
            "actions": [],
        }

    def list_system_status(self, **filters):
        self.filters = filters
        return self._page(self.rows["system"], filters)

    def list_events(self, **filters):
        self.filters = filters
        return self._page(self.rows["events"], filters)

    def list_actions(self, **filters):
        self.filters = filters
        return self._page(self.rows["actions"], filters)

    @staticmethod
    def _page(rows, filters):
        page = filters.get("page", 1)
        page_size = filters.get("page_size", 50)
        return {
            "items": list(rows),
            "page": page,
            "page_size": page_size,
            "total": len(rows),
            "total_pages": 1 if rows else 0,
        }

    def insert_action(self, **values):
        if self.insert_error is not None:
            raise self.insert_error
        self.actions.append(values)
        return len(self.actions)

    def list_gallery(self, **filters):
        self.gallery_filters = filters
        source = filters["source"]
        rows = [
            row
            for row in self.gallery_rows
            if not (
                row["source"] == "event"
                and row["record_id"] in self.deleted_event_images
            )
            and not (
                row["source"] == "action"
                and row["record_id"] in self.deleted_action_images
            )
        ]
        if source == "all":
            return rows
        return [row for row in rows if row["source"] == source]

    def get_event_image_path(self, event_id):
        if event_id in self.deleted_event_images:
            return None
        return self.event_images.get(event_id)

    def get_action_image_path(self, action_id):
        if action_id in self.deleted_action_images:
            return None
        return self.action_images.get(action_id)

    def soft_delete_event_image(self, event_id):
        if self.soft_delete_error is not None:
            raise self.soft_delete_error
        if not self.get_event_image_path(event_id):
            return False
        self.deleted_event_images.add(event_id)
        return True

    def soft_delete_action_image(self, action_id):
        if self.soft_delete_error is not None:
            raise self.soft_delete_error
        if not self.get_action_image_path(action_id):
            return False
        self.deleted_action_images.add(action_id)
        return True

    def preview_log_soft_delete(self, **selection):
        if self.log_soft_delete_error is not None:
            raise self.log_soft_delete_error
        self.log_soft_delete_calls.append(("preview", selection))
        return dict(self.log_soft_delete_preview)

    def soft_delete_logs(self, **selection):
        if self.log_soft_delete_error is not None:
            raise self.log_soft_delete_error
        self.log_soft_delete_calls.append(("delete", selection))
        return dict(self.log_soft_delete_result)

    def set_event_false_alarm(self, event_id, value):
        if self.false_alarm_error is not None:
            raise self.false_alarm_error
        if event_id not in self.false_alarm_events:
            return False
        if self.false_alarm_events[event_id] is value:
            return False
        self.false_alarm_events[event_id] = value
        return True

    def mark_event_reported(self, event_id):
        self.reported.append(event_id)
        return True

    def event_exists(self, event_id):
        if self.false_alarm_error is not None:
            raise self.false_alarm_error
        return event_id in self.false_alarm_events


class PassthroughPrivacyProcessor:
    def process(self, frame):
        return frame.copy()


class FakeImageStore:
    def __init__(self, project_root):
        self.project_root = Path(project_root).resolve()
        self.gallery_root = (self.project_root / "received_frames" / "gallery").resolve()
        self.saved = []
        self.deleted = []
        self.fail_save = False

    def save_image(self, category, frame):
        if self.fail_save:
            raise RuntimeError("privacy save failed")
        assert category in {"events", "actions"}
        assert frame.flags.writeable is False
        relative_path = f"received_frames/gallery/{category}/{len(self.saved) + 1}.jpg"
        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"private-jpeg")
        self.saved.append(relative_path)
        return relative_path

    def resolve(self, relative_path):
        supplied = Path(relative_path)
        if supplied.is_absolute() or ".." in supplied.parts:
            raise UnsafeMediaPathError("unsafe")
        target = (self.project_root / supplied).resolve()
        try:
            target.relative_to(self.gallery_root)
        except ValueError as exc:
            raise UnsafeMediaPathError("unsafe") from exc
        return target

    def delete(self, relative_path):
        self.deleted.append(relative_path)
        self.resolve(relative_path).unlink(missing_ok=True)
        return True


@pytest.fixture
def api(tmp_path):
    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:
        pytest.skip(f"FastAPI test dependencies are unavailable: {exc}")
    server = importlib.import_module("server.app")
    client = TestClient(server.app)
    fake_database = FakeDatabase()
    original_database = server.database
    original_cache = server.frozen_frame_cache
    original_store = server.private_image_store
    original_processor = server.privacy_processor
    original_frame = server.current_frame
    original_frame_seq = server.current_frame_seq
    server.database = fake_database
    server.frozen_frame_cache = FrozenFrameCache(ttl_sec=30)
    server.private_image_store = FakeImageStore(tmp_path)
    server.privacy_processor = PassthroughPrivacyProcessor()
    server.current_frame = None
    server.current_frame_seq = 0
    client.cookies.clear()
    try:
        yield server, client, fake_database
    finally:
        server.database = original_database
        server.frozen_frame_cache = original_cache
        server.private_image_store = original_store
        server.privacy_processor = original_processor
        server.current_frame = original_frame
        server.current_frame_seq = original_frame_seq
        client.close()


def login(server, client):
    csrf = client.get("/api/auth/csrf").json()["csrf_token"]
    user = {"user_id": 11, "email": "tester@example.com", "name": "tester"}
    with patch.object(server, "call_auth", AsyncMock(return_value=(user, None))):
        response = client.post(
            "/api/auth/login",
            headers={"X-CSRF-Token": csrf},
            json={"login_id": "tester@example.com", "password": "Password1"},
        )
    assert response.status_code == 200
    return client.get("/api/auth/csrf").json()["csrf_token"]


def set_current_frame(server, value=80):
    frame = np.full((8, 10, 3), value, dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    with server.state_lock:
        server.current_frame = encoded.tobytes()
        server.current_frame_seq += 1
    return frame


def preview_token(client, csrf):
    response = client.post(
        "/api/logs/current-situation/preview",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200
    return response.headers["X-Frame-Token"]


def test_read_apis_require_authentication_and_empty_is_200(api):
    server, client, database = api
    assert client.get("/api/logs/system-status").status_code == 401
    login(server, client)
    response = client.get("/api/logs/system-status")
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "page": 1,
        "page_size": 50,
        "total": 0,
        "total_pages": 0,
    }


def test_read_api_filters_sorts_and_paginates_on_the_server(api):
    server, client, database = api
    login(server, client)
    response = client.get(
        "/api/logs/events",
        params={
            "start_at": "2026-08-01T00:00:00Z",
            "end_at": "2026-08-14T23:59:59Z",
            "page": "2",
            "page_size": "50",
            "sort_by": "confidence",
            "sort_direction": "asc",
            "event_type": "INTRUSION",
            "is_reported": "true",
        },
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["event_id"] == 7
    assert response.json()["page_size"] == 50
    assert database.filters["page"] == 2
    assert database.filters["page_size"] == 50
    assert database.filters["sort_by"] == "confidence"
    assert database.filters["sort_direction"] == "asc"
    assert database.filters["event_type"] == "INTRUSION"
    assert database.filters["is_reported"] is True
    assert database.filters["start_at"] == datetime(2026, 8, 1)
    assert database.filters["end_at"] == datetime(2026, 8, 14, 23, 59, 59)
    assert client.get("/api/logs/events?page_size=25").status_code == 400
    assert client.get("/api/logs/events?sort_by=event_source").status_code == 400


def test_action_log_filters_use_name_partial_search_and_actual_enum(api):
    server, client, database = api
    login(server, client)
    response = client.get(
        "/api/logs/actions",
        params={"user_name": "홍길", "action_type": "warning"},
    )
    assert response.status_code == 200
    assert database.filters["user_name"] == "홍길"
    assert database.filters["action_type"] == "WARNING"
    assert client.get("/api/logs/actions?action_type=UNKNOWN").status_code == 400


def test_database_unavailable_is_clear_503(api):
    server, client, database = api
    login(server, client)
    database.list_events = Mock(side_effect=DatabaseConfigurationError("offline"))
    response = client.get("/api/logs/events")
    assert response.status_code == 503
    assert response.json()["detail"] == "Database is unavailable."


def test_log_soft_delete_preview_and_delete_require_auth_and_csrf(api):
    server, client, database = api
    payload = {"event_ids": [7], "action_ids": []}
    assert client.post("/api/logs/delete/preview", json=payload).status_code == 401
    csrf = login(server, client)
    assert client.post("/api/logs/delete/preview", json=payload).status_code == 403

    preview = client.post(
        "/api/logs/delete/preview",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    deleted = client.post(
        "/api/logs/delete",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )

    assert preview.status_code == 200
    assert deleted.status_code == 200
    assert database.log_soft_delete_calls == [
        ("preview", payload),
        ("delete", payload),
    ]


def test_log_soft_delete_exposes_stable_counts_and_preserves_files(api):
    server, client, _database = api
    csrf = login(server, client)
    payload = {"event_ids": [7, 8], "action_ids": []}

    preview = client.post(
        "/api/logs/delete/preview",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    ).json()
    deleted = client.post(
        "/api/logs/delete",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    ).json()

    expected = {"events": 2, "actions": 5, "images": 4}
    assert preview == {"ok": True, "selection": payload, "counts": expected}
    assert deleted == {"ok": True, "selection": payload, "counts": expected}
    assert server.private_image_store.deleted == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"event_ids": [], "action_ids": []},
        {"event_ids": [0], "action_ids": []},
        {"event_ids": [7, 7], "action_ids": []},
        {"event_ids": [], "action_ids": "11"},
        {"event_ids": [True], "action_ids": []},
    ],
)
def test_log_soft_delete_rejects_invalid_selection(api, payload):
    server, client, database = api
    csrf = login(server, client)
    response = client.post(
        "/api/logs/delete/preview",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    assert response.status_code == 400
    assert database.log_soft_delete_calls == []


def test_log_soft_delete_database_unavailable_is_503(api):
    server, client, database = api
    csrf = login(server, client)
    database.log_soft_delete_error = DatabaseConfigurationError("offline")
    response = client.post(
        "/api/logs/delete",
        headers={"X-CSRF-Token": csrf},
        json={"event_ids": [], "action_ids": [10]},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Database is unavailable."


def test_action_api_uses_enum_and_has_no_cooldown(api):
    server, client, database = api
    csrf = login(server, client)
    for _ in range(2):
        response = client.post(
            "/api/logs/actions",
            headers={"X-CSRF-Token": csrf},
            json={"action_type": "NOTE", "event_id": None, "description": "checked"},
        )
        assert response.status_code == 201
    assert len(database.actions) == 2
    assert all(action["action_type"] == "NOTE" for action in database.actions)
    invalid = client.post(
        "/api/logs/actions",
        headers={"X-CSRF-Token": csrf},
        json={"action_type": "NEW_ENUM"},
    )
    assert invalid.status_code == 400


def test_manual_report_is_authenticated_csrf_protected_and_not_cooled_down(api):
    server, client, database = api
    original_token, original_chat = server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID
    server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID = "test-token", "test-chat"
    response = Mock(status_code=200)
    try:
        assert client.post("/send_telegram", json={}).status_code == 401
        csrf = login(server, client)
        assert client.post("/send_telegram", json={}).status_code == 403
        with patch.object(server.requests, "post", return_value=response) as sender:
            for _ in range(2):
                result = client.post(
                    "/send_telegram",
                    headers={"X-CSRF-Token": csrf},
                    json={"event_id": 7},
                )
                assert result.status_code == 200
        assert sender.call_count == 2
        assert len(database.actions) == 2
        assert database.reported == [7, 7]
    finally:
        server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID = original_token, original_chat


def test_manual_report_does_not_send_for_missing_event(api):
    server, client, database = api
    original_token, original_chat = server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID
    server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID = "test-token", "test-chat"
    try:
        csrf = login(server, client)
        with patch.object(server.requests, "post") as sender:
            result = client.post(
                "/send_telegram",
                headers={"X-CSRF-Token": csrf},
                json={"event_id": 999},
            )
        assert result.status_code == 404
        sender.assert_not_called()
        assert database.actions == []
    finally:
        server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID = original_token, original_chat


def test_manual_report_returns_success_if_post_send_event_update_fails(api):
    server, client, database = api
    original_token, original_chat = server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID
    server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID = "test-token", "test-chat"
    database.mark_event_reported = Mock(
        side_effect=DatabaseConfigurationError("offline")
    )
    try:
        csrf = login(server, client)
        with patch.object(server.requests, "post", return_value=Mock(status_code=200)):
            result = client.post(
                "/send_telegram",
                headers={"X-CSRF-Token": csrf},
                json={"event_id": 7},
            )
        assert result.status_code == 200
        payload = result.json()
        assert payload["status"] == "success"
        assert "persistence_warning" in payload
        assert len(database.actions) == 1
    finally:
        server.TELEGRAM_TOKEN, server.TELEGRAM_CHAT_ID = original_token, original_chat


def test_current_situation_preview_requires_auth_and_csrf_and_handles_no_frame(api):
    server, client, _database = api
    assert client.post("/api/logs/current-situation/preview").status_code == 401
    csrf = login(server, client)
    assert client.post("/api/logs/current-situation/preview").status_code == 403
    response = client.post(
        "/api/logs/current-situation/preview",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 503


def test_current_situation_preview_returns_private_jpeg_and_frozen_token(api):
    server, client, _database = api
    csrf = login(server, client)
    set_current_frame(server)

    response = client.post(
        "/api/logs/current-situation/preview",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["X-Frame-Token"]
    decoded = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None


def test_current_situation_save_requires_auth_and_csrf(api):
    server, client, database = api
    payload = {"description_content": "note", "include_image": False}
    assert client.post("/api/logs/current-situation", json=payload).status_code == 401
    login(server, client)
    assert client.post("/api/logs/current-situation", json=payload).status_code == 403
    assert database.actions == []


def test_current_situation_rejects_neither_text_nor_image_and_missing_token(api):
    server, client, database = api
    csrf = login(server, client)
    headers = {"X-CSRF-Token": csrf}

    neither = client.post(
        "/api/logs/current-situation",
        headers=headers,
        json={"description_content": "  ", "include_image": False, "frame_token": None},
    )
    missing_token = client.post(
        "/api/logs/current-situation",
        headers=headers,
        json={"description_content": "note", "include_image": True},
    )

    assert neither.status_code == 400
    assert missing_token.status_code == 400
    assert database.actions == []


def test_current_situation_text_only_success_normalizes_description(api):
    server, client, database = api
    csrf = login(server, client)
    response = client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json={"description_content": "  entrance checked  ", "include_image": False},
    )

    assert response.status_code == 201
    assert response.json() == {"ok": True, "action_id": 1, "image_url": None}
    assert database.actions == [
        {
            "user_id": 11,
            "event_id": None,
            "action_type": "NOTE",
            "description": "entrance checked",
            "image_path": None,
        }
    ]


@pytest.mark.parametrize("description", ["", "camera and text"])
def test_current_situation_image_only_and_text_image_success(api, description):
    server, client, database = api
    csrf = login(server, client)
    set_current_frame(server)
    token = preview_token(client, csrf)

    response = client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json={
            "description_content": description,
            "include_image": True,
            "frame_token": token,
        },
    )

    assert response.status_code == 201
    assert response.json()["image_url"] == "/api/media/actions/1"
    assert database.actions[0]["description"] == (description or None)
    assert database.actions[0]["image_path"].startswith(
        "received_frames/gallery/actions/"
    )


def test_current_situation_rejects_invalid_expired_and_wrong_owner_tokens(api):
    server, client, database = api
    csrf = login(server, client)
    set_current_frame(server)
    payload = {"description_content": "", "include_image": True}

    invalid = client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json={**payload, "frame_token": "invalid"},
    )
    wrong_owner_token = server.frozen_frame_cache.store(
        np.zeros((2, 2, 3), dtype=np.uint8),
        user_id=999,
        session_id="another-session",
    )
    wrong_owner = client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json={**payload, "frame_token": wrong_owner_token},
    )

    now = [10.0]
    server.frozen_frame_cache = FrozenFrameCache(ttl_sec=1, clock=lambda: now[0])
    expired_token = server.frozen_frame_cache.store(
        np.zeros((2, 2, 3), dtype=np.uint8),
        user_id=11,
        session_id=csrf,
    )
    now[0] = 11.0
    expired = client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json={**payload, "frame_token": expired_token},
    )

    assert invalid.status_code == 400
    assert wrong_owner.status_code == 400
    assert expired.status_code == 400
    assert database.actions == []


def test_current_situation_image_save_failure_prevents_database_insert(api):
    server, client, database = api
    csrf = login(server, client)
    set_current_frame(server)
    token = preview_token(client, csrf)
    server.private_image_store.fail_save = True

    response = client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json={"description_content": "", "include_image": True, "frame_token": token},
    )

    assert response.status_code == 500
    assert database.actions == []


def test_current_situation_database_failure_cleans_saved_image(api):
    server, client, database = api
    csrf = login(server, client)
    set_current_frame(server)
    token = preview_token(client, csrf)
    database.insert_error = DatabaseConfigurationError("offline")

    response = client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json={"description_content": "note", "include_image": True, "frame_token": token},
    )

    assert response.status_code == 503
    assert len(server.private_image_store.deleted) == 1
    assert not server.private_image_store.resolve(
        server.private_image_store.deleted[0]
    ).exists()


def test_current_situation_success_invalidates_token(api):
    server, client, _database = api
    csrf = login(server, client)
    set_current_frame(server)
    token = preview_token(client, csrf)
    payload = {"description_content": "", "include_image": True, "frame_token": token}

    assert client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    ).status_code == 201
    assert client.post(
        "/api/logs/current-situation",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    ).status_code == 400


def test_gallery_requires_auth_filters_sources_and_hides_raw_paths(api):
    server, client, database = api
    for relative_path in (
        "received_frames/gallery/events/7.jpg",
        "received_frames/gallery/actions/8.jpg",
    ):
        target = server.private_image_store.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"private-jpeg")
    database.gallery_rows = [
        {
            "source": "event",
            "record_id": 7,
            "event_id": 7,
            "action_id": None,
            "event_type": "ASSAULT",
            "image_path": "received_frames/gallery/events/7.jpg",
            "confidence": 0.9,
            "recorded_at": datetime(2026, 8, 23, 10),
        },
        {
            "source": "action",
            "record_id": 8,
            "event_id": None,
            "action_id": 8,
            "action_type": "NOTE",
            "description_content": "checked",
            "image_path": "received_frames/gallery/actions/8.jpg",
            "recorded_at": datetime(2026, 8, 23, 11),
        },
    ]
    assert client.get("/api/gallery").status_code == 401
    login(server, client)

    all_rows = client.get("/api/gallery?source=all")
    event_rows = client.get("/api/gallery?source=event")
    action_rows = client.get("/api/gallery?source=action")

    assert all_rows.status_code == 200 and len(all_rows.json()) == 2
    assert len(event_rows.json()) == 1 and event_rows.json()[0]["source_type"] == "EVENT"
    assert len(action_rows.json()) == 1 and action_rows.json()[0]["source_type"] == "ACTION"
    assert all("image_path" not in row for row in all_rows.json())
    assert all(row["has_image"] is True for row in all_rows.json())
    assert all_rows.json()[0]["image_url"] == "/api/media/events/7"
    assert all_rows.json()[1]["image_url"] == "/api/media/actions/8"
    assert all_rows.json()[0]["source_id"] == 7
    assert all_rows.json()[0]["created_at"] == "2026-08-23T10:00:00"
    assert client.get("/api/gallery?source=unknown").status_code == 400


def test_gallery_omits_database_paths_without_physical_images(api):
    server, client, database = api
    existing_path = "received_frames/gallery/events/existing.jpg"
    target = server.private_image_store.resolve(existing_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"private-jpeg")
    database.gallery_rows = [
        {
            "source": "event",
            "record_id": 3,
            "event_id": 3,
            "image_path": "received_frames/gallery/events/missing-3.jpg",
            "recorded_at": datetime(2026, 8, 23, 9),
        },
        {
            "source": "event",
            "record_id": 5,
            "event_id": 5,
            "image_path": existing_path,
            "recorded_at": datetime(2026, 8, 23, 10),
        },
    ]
    login(server, client)

    response = client.get("/api/gallery?source=event")

    assert response.status_code == 200
    assert [row["source_id"] for row in response.json()] == [5]
    assert response.json()[0]["image_url"] == "/api/media/events/5"


def test_authenticated_media_serves_event_and_action_private_images(api):
    server, client, database = api
    event_path = "received_frames/gallery/events/event-7.jpg"
    action_path = "received_frames/gallery/actions/action-8.jpg"
    for relative_path in (event_path, action_path):
        target = server.private_image_store.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"private-jpeg")
    database.event_images[7] = event_path
    database.action_images[8] = action_path

    assert client.get("/api/media/events/7").status_code == 401
    login(server, client)
    event = client.get("/api/media/events/7")
    action = client.get("/api/media/actions/8")
    assert event.status_code == 200 and event.content == b"private-jpeg"
    assert action.status_code == 200 and action.content == b"private-jpeg"


def test_authenticated_media_rejects_missing_and_unsafe_paths(api):
    server, client, database = api
    login(server, client)
    database.event_images[1] = None
    database.event_images[2] = "received_frames/gallery/events/missing.jpg"
    database.event_images[3] = "../secret.jpg"
    database.action_images[4] = "received_frames/gallery/events/wrong-category.jpg"

    assert client.get("/api/media/events/999").status_code == 404
    assert client.get("/api/media/events/1").status_code == 404
    assert client.get("/api/media/events/2").status_code == 404
    assert client.get("/api/media/events/3").status_code == 404
    assert client.get("/api/media/actions/4").status_code == 404


def test_gallery_delete_requires_authentication(api):
    _server, client, _database = api
    assert client.delete("/api/gallery/event/7").status_code == 401


@pytest.mark.parametrize("csrf_header", [None, "invalid"])
def test_gallery_delete_requires_valid_csrf(api, csrf_header):
    server, client, database = api
    csrf = login(server, client)
    database.event_images[7] = "received_frames/gallery/events/7.jpg"
    headers = {} if csrf_header is None else {"X-CSRF-Token": csrf_header}

    response = client.delete("/api/gallery/event/7", headers=headers)

    assert response.status_code == 403
    assert 7 not in database.deleted_event_images
    assert csrf


def test_gallery_delete_rejects_invalid_source(api):
    server, client, _database = api
    csrf = login(server, client)
    response = client.delete(
        "/api/gallery/unknown/7",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400


def test_gallery_delete_event_soft_deletes_without_removing_file(api):
    server, client, database = api
    csrf = login(server, client)
    image_path = "received_frames/gallery/events/event-7.jpg"
    target = server.private_image_store.resolve(image_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"private-jpeg")
    database.event_images[7] = image_path
    database.gallery_rows = [
        {
            "source": "event",
            "record_id": 7,
            "event_id": 7,
            "image_path": image_path,
            "recorded_at": datetime(2026, 8, 24, 10),
        }
    ]

    response = client.delete(
        "/api/gallery/event/7",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "source": "event", "record_id": 7}
    assert target.is_file()
    assert server.private_image_store.deleted == []
    assert client.get("/api/gallery").json() == []
    assert client.get("/api/media/events/7").status_code == 404


def test_gallery_delete_action_success(api):
    server, client, database = api
    csrf = login(server, client)
    database.action_images[8] = "received_frames/gallery/actions/action-8.jpg"

    response = client.delete(
        "/api/gallery/action/8",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "source": "action", "record_id": 8}
    assert 8 in database.deleted_action_images


@pytest.mark.parametrize("record_id", [999, 10])
def test_gallery_delete_missing_or_no_active_image_is_404(api, record_id):
    server, client, database = api
    csrf = login(server, client)
    if record_id == 10:
        database.event_images[record_id] = None

    response = client.delete(
        f"/api/gallery/event/{record_id}",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "error",
    [DatabaseConfigurationError("offline"), DatabaseOperationError("failed")],
)
def test_gallery_delete_database_unavailable_is_503(api, error):
    server, client, database = api
    csrf = login(server, client)
    database.event_images[7] = "received_frames/gallery/events/7.jpg"
    database.soft_delete_error = error

    response = client.delete(
        "/api/gallery/event/7",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 503


def test_false_alarm_patch_requires_authentication(api):
    _server, client, _database = api
    response = client.patch(
        "/api/logs/events/7/false-alarm",
        json={"is_false_alarm": True},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("csrf_header", [None, "invalid"])
def test_false_alarm_patch_requires_valid_csrf(api, csrf_header):
    server, client, _database = api
    csrf = login(server, client)
    headers = {} if csrf_header is None else {"X-CSRF-Token": csrf_header}

    response = client.patch(
        "/api/logs/events/7/false-alarm",
        headers=headers,
        json={"is_false_alarm": True},
    )

    assert response.status_code == 403
    assert csrf


def test_false_alarm_patch_rejects_invalid_json(api):
    server, client, _database = api
    csrf = login(server, client)
    response = client.patch(
        "/api/logs/events/7/false-alarm",
        headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
        content="{",
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [{}, {"is_false_alarm": "true"}, {"is_false_alarm": 1}],
)
def test_false_alarm_patch_requires_actual_boolean(api, payload):
    server, client, _database = api
    csrf = login(server, client)
    response = client.patch(
        "/api/logs/events/7/false-alarm",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("value", [True, False])
def test_false_alarm_patch_updates_true_and_false(api, value):
    server, client, database = api
    csrf = login(server, client)
    database.false_alarm_events[7] = not value

    response = client.patch(
        "/api/logs/events/7/false-alarm",
        headers={"X-CSRF-Token": csrf},
        json={"is_false_alarm": value},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "event_id": 7,
        "is_false_alarm": value,
    }
    assert database.false_alarm_events[7] is value


def test_false_alarm_patch_missing_event_is_404(api):
    server, client, _database = api
    csrf = login(server, client)
    response = client.patch(
        "/api/logs/events/999/false-alarm",
        headers={"X-CSRF-Token": csrf},
        json={"is_false_alarm": True},
    )
    assert response.status_code == 404


def test_false_alarm_patch_same_value_is_idempotent_success(api):
    server, client, database = api
    csrf = login(server, client)
    database.false_alarm_events[7] = True

    response = client.patch(
        "/api/logs/events/7/false-alarm",
        headers={"X-CSRF-Token": csrf},
        json={"is_false_alarm": True},
    )

    assert response.status_code == 200
    assert response.json()["is_false_alarm"] is True


@pytest.mark.parametrize(
    "error",
    [DatabaseConfigurationError("offline"), DatabaseOperationError("failed")],
)
def test_false_alarm_patch_database_unavailable_is_503(api, error):
    server, client, database = api
    csrf = login(server, client)
    database.false_alarm_error = error

    response = client.patch(
        "/api/logs/events/7/false-alarm",
        headers={"X-CSRF-Token": csrf},
        json={"is_false_alarm": True},
    )

    assert response.status_code == 503


@pytest.mark.parametrize(
    "path",
    ["/api/logs/system-status", "/api/logs/events", "/api/logs/actions"],
)
def test_all_log_routes_forward_start_at_and_end_at(api, path):
    server, client, database = api
    login(server, client)
    response = client.get(
        path,
        params={
            "start_at": "2026-08-01T00:00:00Z",
            "end_at": "2026-08-24T23:59:59Z",
        },
    )
    assert response.status_code == 200
    assert database.filters["start_at"] == datetime(2026, 8, 1)
    assert database.filters["end_at"] == datetime(2026, 8, 24, 23, 59, 59)


def test_existing_log_responses_hide_removed_and_private_path_fields(api):
    server, client, database = api
    for relative_path in (
        "received_frames/gallery/events/7.jpg",
        "received_frames/gallery/actions/8.jpg",
    ):
        target = server.private_image_store.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"private-jpeg")
    database.rows["system"] = [{"status_id": 1, "lidar_z": 3.5}]
    database.rows["events"] = [
        {
            "event_id": 7,
            "image_path": "received_frames/gallery/events/7.jpg",
            "video_path": "private.mp4",
            "lidar_z": 2.0,
        }
    ]
    database.rows["actions"] = [
        {
            "action_id": 8,
            "image_path": "received_frames/gallery/actions/8.jpg",
        }
    ]
    login(server, client)

    status = client.get("/api/logs/system-status").json()["items"][0]
    event = client.get("/api/logs/events").json()["items"][0]
    action = client.get("/api/logs/actions").json()["items"][0]
    assert "lidar_z" not in status
    assert "image_path" not in event and "video_path" not in event and "lidar_z" not in event
    assert event["has_image"] is True
    assert event["image_url"] == "/api/media/events/7"
    assert "image_path" not in action
    assert action["has_image"] is True
    assert action["image_url"] == "/api/media/actions/8"


def test_log_responses_do_not_advertise_missing_physical_images(api):
    server, client, database = api
    database.rows["events"] = [
        {
            "event_id": 3,
            "image_path": "received_frames/gallery/events/missing-3.jpg",
        },
        {
            "event_id": 4,
            "image_path": "received_frames/gallery/events/missing-4.jpg",
        },
    ]
    database.rows["actions"] = [
        {
            "action_id": 4,
            "image_path": "received_frames/gallery/actions/missing-4.jpg",
        }
    ]
    login(server, client)

    events = client.get("/api/logs/events").json()["items"]
    actions = client.get("/api/logs/actions").json()["items"]

    assert [(row["has_image"], row["image_url"]) for row in events] == [
        (False, None),
        (False, None),
    ]
    assert actions[0]["has_image"] is False
    assert actions[0]["image_url"] is None


def test_automatic_event_delegates_frame_and_uses_only_worker_cooldown(api):
    server, _client, _database = api

    class RecordingWorker:
        def __init__(self):
            self.calls = []

        def submit(self, **values):
            self.calls.append(values)
            return True

    original_worker = server.event_log_worker
    worker = RecordingWorker()
    server.event_log_worker = worker
    try:
        set_current_frame(server, value=120)
        assert server.submit_automatic_event("VISION_AI", "ASSAULT") is True
        assert server.submit_automatic_event("VISION_AI", "ASSAULT") is True
        assert isinstance(worker.calls[0]["frame"], np.ndarray)
        assert len(worker.calls) == 2
        assert server.submit_automatic_event("SYSTEM_MONITOR", "SYSTEM_ERROR") is True
        assert worker.calls[-1]["frame"] is None
    finally:
        server.event_log_worker = original_worker
