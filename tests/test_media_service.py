from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from server.logging_service import EventLogWorker
from server.media_service import (
    FrozenFrameCache,
    FrozenFrameTokenError,
    PrivateImageStore,
    UnsafeMediaPathError,
)


class RecordingPrivacyProcessor:
    def __init__(self):
        self.frames = []

    def save_image(self, path, frame):
        assert frame.flags.writeable is False
        self.frames.append(frame.copy())
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"private-jpeg")
        return target


class RecordingDatabase:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    def insert_event(self, **values):
        if self.fail:
            raise RuntimeError("database unavailable")
        self.events.append(values)
        return len(self.events)


class RecordingNotifier:
    def __init__(self):
        self.alerts = []

    def send_event_alert_async(self, message, **values):
        self.alerts.append((message, values))


class RecordingImageStore:
    def __init__(self, *, fail_save=False):
        self.saved = []
        self.deleted = []
        self.fail_save = fail_save

    def save_image(self, category, frame):
        if self.fail_save:
            raise RuntimeError("privacy save failed")
        assert category == "events"
        assert frame.flags.writeable is False
        self.saved.append(frame.copy())
        return f"received_frames/gallery/events/{len(self.saved)}.jpg"

    def delete(self, relative_path):
        self.deleted.append(relative_path)
        return True


@pytest.mark.parametrize("category", ["events", "actions"])
def test_private_image_store_returns_safe_relative_path_and_removes_file(
    tmp_path, category
):
    processor = RecordingPrivacyProcessor()
    store = PrivateImageStore(project_root=tmp_path, processor=processor)
    frame = np.full((4, 5, 3), 17, dtype=np.uint8)
    frame.setflags(write=False)

    relative_path = store.save_image(category, frame)

    assert relative_path.startswith(f"received_frames/gallery/{category}/")
    assert not Path(relative_path).is_absolute()
    assert store.resolve(relative_path).read_bytes() == b"private-jpeg"
    assert np.array_equal(processor.frames[0], frame)
    assert store.delete(relative_path) is True
    assert not store.resolve(relative_path).exists()


@pytest.mark.parametrize(
    "unsafe_path",
    ["../secret.jpg", "received_frames/gallery/../secret.jpg"],
)
def test_private_image_store_rejects_paths_outside_gallery(tmp_path, unsafe_path):
    store = PrivateImageStore(
        project_root=tmp_path,
        processor=RecordingPrivacyProcessor(),
    )

    with pytest.raises(UnsafeMediaPathError):
        store.resolve(unsafe_path)


def test_frozen_frame_cache_binds_owner_preserves_sequence_and_invalidates():
    now = [10.0]
    cache = FrozenFrameCache(ttl_sec=5, clock=lambda: now[0])
    source = np.arange(18, dtype=np.uint8).reshape((2, 3, 3))
    token = cache.store(
        source,
        user_id=7,
        session_id="session-a",
        frame_sequence=42,
    )
    source[:] = 0

    with pytest.raises(FrozenFrameTokenError):
        cache.get(token, user_id=8, session_id="session-a")
    with pytest.raises(FrozenFrameTokenError):
        cache.get(token, user_id=7, session_id="session-b")

    frozen = cache.get(token, user_id=7, session_id="session-a")
    assert frozen.frame_sequence == 42
    assert frozen.frame.flags.writeable is False
    assert np.array_equal(frozen.frame, np.arange(18, dtype=np.uint8).reshape((2, 3, 3)))

    cache.invalidate(token, user_id=7, session_id="session-a")
    with pytest.raises(FrozenFrameTokenError):
        cache.get(token, user_id=7, session_id="session-a")


def test_frozen_frame_cache_uses_random_tokens_and_rejects_expired_tokens():
    now = [100.0]
    cache = FrozenFrameCache(ttl_sec=2, clock=lambda: now[0])
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    first = cache.store(frame, user_id=1, session_id="s")
    second = cache.store(frame, user_id=1, session_id="s")
    assert first != second
    assert len(first) >= 32

    now[0] = 102.0
    with pytest.raises(FrozenFrameTokenError):
        cache.get(first, user_id=1, session_id="s")


def test_event_worker_uses_one_cooldown_for_one_event_and_at_most_one_image():
    database = RecordingDatabase()
    image_store = RecordingImageStore()
    worker = EventLogWorker(
        database,
        RecordingNotifier(),
        cooldown_sec=10,
        image_store=image_store,
    )
    worker.gate.clock = lambda: 10.0
    frame = np.full((3, 3, 3), 99, dtype=np.uint8)
    worker.start()
    try:
        assert worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
            confidence=0.9,
            location={"lidar_x": 1, "lidar_y": 2, "lidar_z": 3},
            frame=frame,
        )
        frame[:] = 0
        assert not worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
            frame=frame,
        )
        worker.queue.join()
    finally:
        worker.stop()

    assert len(database.events) == 1
    assert len(image_store.saved) == 1
    assert np.all(image_store.saved[0] == 99)
    assert database.events[0]["image_path"].startswith(
        "received_frames/gallery/events/"
    )
    assert database.events[0]["lidar_x"] == 1
    assert database.events[0]["lidar_y"] == 2
    assert "lidar_z" not in database.events[0]


def test_system_monitor_event_is_saved_without_an_image():
    database = RecordingDatabase()
    image_store = RecordingImageStore()
    worker = EventLogWorker(
        database,
        RecordingNotifier(),
        cooldown_sec=0,
        image_store=image_store,
    )
    worker.start()
    try:
        assert worker.submit(
            robot_id="robot-1",
            event_source="SYSTEM_MONITOR",
            event_type="SYSTEM_ERROR",
        )
        worker.queue.join()
    finally:
        worker.stop()

    assert database.events[0]["image_path"] is None
    assert image_store.saved == []


def test_event_worker_removes_saved_image_when_database_insert_fails():
    image_store = RecordingImageStore()
    worker = EventLogWorker(
        RecordingDatabase(fail=True),
        RecordingNotifier(),
        cooldown_sec=0,
        image_store=image_store,
    )
    worker.start()
    try:
        assert worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="INTRUSION",
            frame=np.zeros((2, 2, 3), dtype=np.uint8),
        )
        worker.queue.join()
    finally:
        worker.stop()

    assert len(image_store.saved) == 1
    assert image_store.deleted == ["received_frames/gallery/events/1.jpg"]


def test_event_worker_does_not_insert_when_requested_image_save_fails():
    database = RecordingDatabase()
    worker = EventLogWorker(
        database,
        RecordingNotifier(),
        cooldown_sec=0,
        image_store=RecordingImageStore(fail_save=True),
    )
    worker.start()
    try:
        assert worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="INTRUSION",
            frame=np.zeros((2, 2, 3), dtype=np.uint8),
        )
        worker.queue.join()
    finally:
        worker.stop()

    assert database.events == []
