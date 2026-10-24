from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import threading
import time

from server.database import Database, DatabaseConfig
from server.logging_service import EventLogWorker, SystemStatusWriter


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executions = []
        self.lastrowid = 41
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.executions.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class PageCursor(FakeCursor):
    def __init__(self, *, total, rows=None):
        super().__init__(rows)
        self.total = total

    def fetchone(self):
        return {"total": self.total}


class SequenceCursor(FakeCursor):
    def __init__(self, result_sets):
        super().__init__()
        self.result_sets = list(result_sets)
        self.current_rows = []

    def execute(self, sql, params):
        super().execute(sql, params)
        if sql.lstrip().upper().startswith("SELECT"):
            self.current_rows = self.result_sets.pop(0)

    def fetchall(self):
        return self.current_rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class DatabaseHarness(Database):
    def __init__(self, cursor):
        super().__init__(DatabaseConfig("host", 3306, "dabom", "user", "pw", 1))
        self.cursor = cursor

    @contextmanager
    def transaction(self):
        yield FakeConnection(self.cursor)


def test_database_previews_mixed_log_soft_delete_without_double_counting():
    cursor = SequenceCursor(
        [
            [
                {
                    "event_id": 7,
                    "image_path": "received_frames/gallery/events/7.jpg",
                    "image_deleted_at": None,
                }
            ],
            [
                {
                    "action_id": 10,
                    "event_id": 7,
                    "image_path": "received_frames/gallery/actions/10.jpg",
                    "image_deleted_at": None,
                },
                {
                    "action_id": 11,
                    "event_id": 9,
                    "image_path": "received_frames/gallery/actions/11.jpg",
                    "image_deleted_at": datetime(2026, 8, 25),
                },
            ],
        ]
    )
    database = DatabaseHarness(cursor)

    counts = database.preview_log_soft_delete(event_ids=[7], action_ids=[10, 11])

    assert counts == {"events": 1, "actions": 2, "images": 2}
    assert len(cursor.executions) == 2
    event_sql, event_params = cursor.executions[0]
    action_sql, action_params = cursor.executions[1]
    assert "FROM event_log" in event_sql
    assert "is_deleted = 0" in event_sql
    assert "FOR UPDATE" not in event_sql
    assert event_params == (7,)
    assert "action_id IN (%s, %s) OR event_id IN (%s)" in action_sql
    assert action_params == (10, 11, 7)
    assert "UPDATE" not in " ".join(sql for sql, _ in cursor.executions)


def test_database_soft_deletes_revalidated_event_and_cascaded_actions():
    cursor = SequenceCursor(
        [
            [
                {
                    "event_id": 7,
                    "image_path": None,
                    "image_deleted_at": None,
                }
            ],
            [
                {
                    "action_id": 10,
                    "event_id": 7,
                    "image_path": "received_frames/gallery/actions/10.jpg",
                    "image_deleted_at": None,
                }
            ],
        ]
    )
    database = DatabaseHarness(cursor)

    counts = database.soft_delete_logs(event_ids=[7], action_ids=[10])

    assert counts == {"events": 1, "actions": 1, "images": 1}
    assert len(cursor.executions) == 4
    assert "FOR UPDATE" in cursor.executions[0][0]
    assert "FOR UPDATE" in cursor.executions[1][0]
    action_update, action_params = cursor.executions[2]
    event_update, event_params = cursor.executions[3]
    assert "UPDATE action_log" in action_update
    assert "SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP" in action_update
    assert "image_deleted_at" not in action_update
    assert action_params == (10,)
    assert "UPDATE event_log" in event_update
    assert "SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP" in event_update
    assert "image_deleted_at" not in event_update
    assert event_params == (7,)


def test_database_rejects_invalid_log_soft_delete_ids():
    database = DatabaseHarness(SequenceCursor([]))

    for event_ids, action_ids in (
        ([], []),
        ([1, 1], []),
        ([True], []),
        ([], [0]),
    ):
        try:
            database.preview_log_soft_delete(
                event_ids=event_ids, action_ids=action_ids
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid soft-delete IDs were accepted")


def test_database_writes_schema_columns_and_nulls_with_bound_parameters():
    cursor = FakeCursor()
    database = DatabaseHarness(cursor)
    status_id = database.insert_system_status(
        {
            "cpu_usage": 20.5,
            "cpu_temperature": None,
            "ram_usage": 33.0,
            "ping": None,
            "is_autonomous": 1,
            "speed": None,
            "gps_lat": None,
            "gps_lng": None,
            "gps_alt": None,
            "lidar_x": None,
            "lidar_y": None,
            "lidar_z": None,
        }
    )
    assert status_id == 41
    sql, params = cursor.executions[0]
    assert "INSERT INTO system_status" in sql
    assert "%s" in sql
    assert params[1] is None
    assert params[3] is None
    assert params[6] is None
    assert "battery" not in sql.lower()
    assert "lidar_z" not in sql


def test_database_inserts_relative_image_paths_without_removed_columns():
    cursor = FakeCursor()
    database = DatabaseHarness(cursor)

    event_id = database.insert_event(
        event_source="VISION_AI",
        event_type="ASSAULT",
        image_path="received_frames/gallery/events/event-1.jpg",
        confidence=0.91,
        lidar_x=1.5,
        lidar_y=2.5,
    )
    action_id = database.insert_action(
        user_id=3,
        event_id=event_id,
        action_type="NOTE",
        description="현장 확인",
        image_path="received_frames/gallery/actions/action-1.jpg",
    )

    assert event_id == 41
    assert action_id == 41
    event_sql, event_params = cursor.executions[0]
    assert "INSERT INTO event_log" in event_sql
    assert "image_path" in event_sql
    assert "video_path" not in event_sql
    assert "lidar_z" not in event_sql
    assert event_params == (
        "VISION_AI",
        "ASSAULT",
        "received_frames/gallery/events/event-1.jpg",
        0.91,
        None,
        None,
        None,
        1.5,
        2.5,
    )
    action_sql, action_params = cursor.executions[1]
    assert "INSERT INTO action_log" in action_sql
    assert "image_path" in action_sql
    assert action_params == (
        3,
        41,
        "NOTE",
        "현장 확인",
        "received_frames/gallery/actions/action-1.jpg",
    )


def test_database_rejects_absolute_or_escaping_image_paths():
    cursor = FakeCursor()
    database = DatabaseHarness(cursor)

    invalid_paths = (
        "C:\\private\\event.jpg",
        "/private/event.jpg",
        "received_frames/gallery/../raw.jpg",
        "",
    )
    for image_path in invalid_paths:
        try:
            database.insert_event(
                event_source="VISION_AI",
                event_type="ASSAULT",
                image_path=image_path,
            )
        except ValueError as exc:
            assert "relative path" in str(exc)
        else:
            raise AssertionError(f"unsafe image path was accepted: {image_path}")
    assert cursor.executions == []


def test_database_filters_counts_sorts_and_pages_events():
    expected = [{"event_id": 2}, {"event_id": 1}]
    cursor = PageCursor(total=52, rows=expected)
    database = DatabaseHarness(cursor)
    start = datetime(2026, 8, 1)
    end = datetime(2026, 8, 14)
    result = database.list_events(
        start_at=start,
        end_at=end,
        event_type="ASSAULT",
        confidence_min=0.5,
        confidence_max=0.9,
        is_resolved=True,
        is_reported=False,
        is_alerted=True,
        is_false_alarm=False,
        page=2,
        page_size=25,
        sort_by="confidence",
        sort_direction="asc",
    )

    assert result == {
        "items": expected,
        "page": 2,
        "page_size": 25,
        "total": 52,
        "total_pages": 3,
    }
    count_sql, count_params = cursor.executions[0]
    select_sql, select_params = cursor.executions[1]
    for sql in (count_sql, select_sql):
        assert "e.is_deleted = 0" in sql
        assert "e.detected_at >= %s" in sql
        assert "e.detected_at <= %s" in sql
        assert "e.event_type = %s" in sql
        assert "e.confidence >= %s" in sql
        assert "e.confidence <= %s" in sql
        assert "e.is_resolved = %s" in sql
        assert "e.is_reported = %s" in sql
        assert "e.is_alerted = %s" in sql
        assert "e.is_false_alarm = %s" in sql
    assert "COUNT(*) AS total" in count_sql
    assert "ORDER BY e.confidence ASC, e.event_id ASC LIMIT %s OFFSET %s" in select_sql
    assert "image_path" in select_sql
    assert "CASE WHEN e.image_deleted_at IS NULL" in select_sql
    assert "video_path" not in select_sql
    assert "lidar_z" not in select_sql
    filter_params = (start, end, "ASSAULT", 0.5, 0.9, 1, 0, 1, 0)
    assert count_params == filter_params
    assert select_params == filter_params + (25, 25)


def test_database_lists_system_status_and_actions_with_dashboard_fields():
    status_cursor = PageCursor(total=51)
    status_database = DatabaseHarness(status_cursor)
    status_page = status_database.list_system_status()
    status_count_sql, status_count_params = status_cursor.executions[0]
    status_sql, status_params = status_cursor.executions[1]
    assert status_page["total_pages"] == 2
    assert "COUNT(*) AS total" in status_count_sql
    assert status_count_params == ()
    assert "lidar_x" in status_sql and "lidar_y" in status_sql
    assert "battery" not in status_sql.lower()
    assert "lidar_z" not in status_sql
    assert "ORDER BY recorded_at DESC, status_id DESC LIMIT %s OFFSET %s" in status_sql
    assert status_params == (50, 0)

    action_cursor = PageCursor(total=16)
    action_database = DatabaseHarness(action_cursor)
    start = datetime(2026, 8, 1)
    end = datetime(2026, 8, 14)
    action_page = action_database.list_actions(
        start_at=start,
        end_at=end,
        user_name="길동",
        action_type="WARNING",
        page=2,
        page_size=15,
        sort_by="user_name",
    )
    action_count_sql, action_count_params = action_cursor.executions[0]
    action_sql, action_params = action_cursor.executions[1]
    assert action_page["total_pages"] == 2
    assert "COUNT(*) AS total" in action_count_sql
    assert "u.name AS user_name" in action_sql
    assert "u.email AS user_email" in action_sql
    assert "CASE WHEN a.image_deleted_at IS NULL" in action_sql
    assert "a.is_deleted = 0" in action_sql
    assert "INSTR(u.name, %s) > 0" in action_sql
    assert "a.action_type = %s" in action_sql
    assert "ORDER BY u.name DESC, a.action_id DESC LIMIT %s OFFSET %s" in action_sql
    assert action_count_params == (start, end, "길동", "WARNING")
    assert action_params == (start, end, "길동", "WARNING", 15, 15)


def test_database_rejects_invalid_record_page_sort_and_confidence_values():
    database = DatabaseHarness(PageCursor(total=0))

    invalid_calls = (
        lambda: database.list_events(page=0),
        lambda: database.list_events(page_size=0),
        lambda: database.list_events(sort_by="event_source"),
        lambda: database.list_events(sort_direction="sideways"),
        lambda: database.list_events(confidence_min=-0.1),
        lambda: database.list_events(confidence_max=1.1),
        lambda: database.list_events(confidence_min=0.8, confidence_max=0.2),
    )
    for call in invalid_calls:
        try:
            call()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid record query value was accepted")


def test_database_lists_gallery_images_from_events_and_actions():
    expected = [
        {
            "source": "event",
            "record_id": 9,
            "image_path": "received_frames/gallery/events/event-9.jpg",
        },
        {
            "source": "action",
            "record_id": 4,
            "image_path": "received_frames/gallery/actions/action-4.jpg",
        },
    ]
    cursor = FakeCursor(expected)
    database = DatabaseHarness(cursor)
    start = datetime(2026, 8, 1)
    end = datetime(2026, 8, 23)

    assert database.list_gallery(
        source="all", start_at=start, end_at=end, limit=12
    ) == expected
    sql, params = cursor.executions[0]
    assert "'event' AS source" in sql
    assert "'action' AS source" in sql
    assert "UNION ALL" in sql
    assert sql.count("image_path IS NOT NULL") == 2
    assert "e.image_deleted_at IS NULL" in sql
    assert "a.image_deleted_at IS NULL" in sql
    assert "ORDER BY recorded_at DESC LIMIT %s" in sql
    assert "video_path" not in sql
    assert "lidar_z" not in sql
    assert params == (start, end, start, end, 12)


def test_database_filters_gallery_source_and_rejects_unknown_source():
    cursor = FakeCursor([])
    database = DatabaseHarness(cursor)

    database.list_gallery(source="event", limit=5)
    event_sql, event_params = cursor.executions[0]
    assert "FROM event_log AS e" in event_sql
    assert "FROM action_log AS a" not in event_sql
    assert event_params == (5,)

    try:
        database.list_gallery(source="unknown")
    except ValueError as exc:
        assert "all, event, action" in str(exc)
    else:
        raise AssertionError("unknown gallery source was accepted")


def test_database_gets_event_and_action_image_paths_by_bound_id():
    event_cursor = FakeCursor(
        [{"image_path": "received_frames/gallery/events/event-7.jpg"}]
    )
    event_database = DatabaseHarness(event_cursor)
    assert event_database.get_event_image_path(7) == (
        "received_frames/gallery/events/event-7.jpg"
    )
    event_sql, event_params = event_cursor.executions[0]
    assert "FROM event_log" in event_sql
    assert "event_id = %s" in event_sql
    assert "is_deleted = 0" in event_sql
    assert "image_deleted_at IS NULL" in event_sql
    assert event_params == (7,)

    action_cursor = FakeCursor(
        [{"image_path": "received_frames/gallery/actions/action-8.jpg"}]
    )
    action_database = DatabaseHarness(action_cursor)
    assert action_database.get_action_image_path(8) == (
        "received_frames/gallery/actions/action-8.jpg"
    )
    action_sql, action_params = action_cursor.executions[0]
    assert "FROM action_log" in action_sql
    assert "action_id = %s" in action_sql
    assert "image_deleted_at IS NULL" in action_sql
    assert action_params == (8,)


def test_database_soft_deletes_only_event_and_action_images():
    cursor = FakeCursor()
    database = DatabaseHarness(cursor)

    assert database.soft_delete_event_image(7) is True
    assert database.soft_delete_action_image(8) is True

    event_sql, event_params = cursor.executions[0]
    action_sql, action_params = cursor.executions[1]
    for sql in (event_sql, action_sql):
        assert "SET image_deleted_at = CURRENT_TIMESTAMP" in sql
        assert "image_deleted_at IS NULL" in sql
        assert "image_path IS NOT NULL" in sql
        assert "SET is_deleted" not in sql
        assert "SET image_path" not in sql
        assert "DELETE FROM" not in sql
    assert "UPDATE event_log" in event_sql
    assert "event_id = %s" in event_sql
    assert event_params == (7,)
    assert "UPDATE action_log" in action_sql
    assert "action_id = %s" in action_sql
    assert action_params == (8,)


def test_database_sets_event_false_alarm_with_bound_values():
    cursor = FakeCursor()
    database = DatabaseHarness(cursor)

    assert database.set_event_false_alarm(9, True) is True
    assert database.set_event_false_alarm(9, False) is True

    true_sql, true_params = cursor.executions[0]
    false_sql, false_params = cursor.executions[1]
    assert "UPDATE event_log" in true_sql
    assert "SET is_false_alarm = %s" in true_sql
    assert "event_id = %s" in true_sql
    assert "is_deleted = 0" in true_sql
    assert true_params == (1, 9)
    assert false_sql == true_sql
    assert false_params == (0, 9)


def test_dashboard_records_schema_and_forward_migration_match_contract():
    repository_root = Path(__file__).resolve().parents[1]
    schema = (repository_root / "data/database/init_schema.sql").read_text(
        encoding="utf-8"
    )
    migration = (
        repository_root
        / "data/database/migrations/20260823_dashboard_records.sql"
    ).read_text(encoding="utf-8")
    followup_migration = (
        repository_root
        / "data/database/migrations/20260823_dashboard_records_followup.sql"
    ).read_text(encoding="utf-8")

    assert schema.count("`image_path` varchar(255) DEFAULT NULL") == 2
    assert schema.count("`image_deleted_at` datetime DEFAULT NULL") == 2
    assert "`is_false_alarm`" in schema
    assert "battery" not in schema.lower()
    assert "`video_path`" not in schema
    assert "`lidar_z`" not in schema
    assert "CHANGE COLUMN `video_path` `image_path`" in migration
    assert "ALTER TABLE `action_log`" in migration
    assert migration.count("DROP COLUMN `lidar_z`") == 2
    assert "DROP TABLE" not in migration
    assert "TRUNCATE" not in migration
    assert followup_migration.count("COLUMN_NAME = 'image_deleted_at'") == 2
    assert followup_migration.count("ADD COLUMN `image_deleted_at`") == 2
    assert followup_migration.count("IF NOT EXISTS") == 2
    assert "DROP TABLE" not in followup_migration
    assert "TRUNCATE" not in followup_migration


def test_battery_status_forward_migration_is_rerun_safe():
    migration = (
        Path(__file__).resolve().parents[1]
        / "data/database/migrations/20260824_remove_battery_status.sql"
    ).read_text(encoding="utf-8")
    assert "TABLE_NAME = 'system_status'" in migration
    assert "COLUMN_NAME = 'battery_level'" in migration
    assert "IF EXISTS" in migration
    assert migration.count("DROP COLUMN `battery_level`") == 1
    assert "DROP TABLE" not in migration
    assert "TRUNCATE" not in migration


def test_database_checks_reportable_event_with_bound_id():
    cursor = FakeCursor([{"exists": 1}])
    database = DatabaseHarness(cursor)
    assert database.event_exists(7) is True
    sql, params = cursor.executions[0]
    assert "event_id = %s" in sql
    assert "is_deleted = 0" in sql
    assert params == (7,)


class RecordingDatabase:
    def __init__(self):
        self.events = []
        self.statuses = []

    def insert_event(self, **values):
        self.events.append(values)
        return len(self.events)

    def insert_system_status(self, values):
        self.statuses.append(values)
        return len(self.statuses)


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def send_event_alert_async(self, message, *, robot_id, event_type):
        self.events.append((robot_id, event_type, message))
        return True


class RaisingNotifier:
    def send_event_alert_async(self, *_args, **_kwargs):
        raise RuntimeError("sender failed")


class BlockingDatabase(RecordingDatabase):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def insert_event(self, **values):
        if not self.events:
            self.entered.set()
            assert self.release.wait(2)
        return super().insert_event(**values)


def test_event_worker_cooldown_is_per_robot_and_event_type():
    database = RecordingDatabase()
    notifier = RecordingNotifier()
    worker = EventLogWorker(database, notifier, cooldown_sec=10)
    now = [100.0]
    worker.gate.clock = lambda: now[0]
    worker.start()
    try:
        assert worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
            confidence=0.9,
        )
        assert not worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
            confidence=0.8,
        )
        assert worker.submit(
            robot_id="robot-1",
            event_source="SYSTEM_MONITOR",
            event_type="NETWORK_LOSS",
        )
        assert worker.submit(
            robot_id="robot-2",
            event_source="VISION_AI",
            event_type="ASSAULT",
        )
        worker.queue.join()
        assert len(database.events) == 3
        now[0] += 10
        assert worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
        )
        worker.queue.join()
        assert len(database.events) == 4
    finally:
        worker.stop()


def test_event_worker_cooldown_can_be_scoped_per_incident():
    database = RecordingDatabase()
    notifier = RecordingNotifier()
    worker = EventLogWorker(database, notifier, cooldown_sec=10)
    now = [100.0]
    worker.gate.clock = lambda: now[0]
    worker.start()
    try:
        assert worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
            confidence=0.9,
            cooldown_key="pair:1:2",
        )
        assert not worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
            confidence=0.8,
            cooldown_key="pair:1:2",
        )
        assert worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
            confidence=0.85,
            cooldown_key="pair:3:4",
        )
        worker.queue.join()
        assert len(database.events) == 2
    finally:
        worker.stop()


def test_event_worker_stops_after_a_full_queue_drains():
    database = BlockingDatabase()
    worker = EventLogWorker(
        database,
        RecordingNotifier(),
        cooldown_sec=0,
        queue_size=1,
    )
    worker.start()
    assert worker.submit(
        robot_id="robot-1",
        event_source="VISION_AI",
        event_type="ASSAULT",
    )
    assert database.entered.wait(1)
    assert worker.submit(
        robot_id="robot-1",
        event_source="SYSTEM_MONITOR",
        event_type="SYSTEM_ERROR",
    )
    stopped = threading.Event()

    def stop_worker():
        worker.stop(timeout_sec=2)
        stopped.set()

    stopper = threading.Thread(target=stop_worker)
    stopper.start()
    time.sleep(0.05)
    database.release.set()
    stopper.join(2)
    assert stopped.is_set()
    assert worker.thread is None
    assert len(database.events) == 2


def test_event_worker_survives_notifier_exception():
    database = RecordingDatabase()
    worker = EventLogWorker(database, RaisingNotifier(), cooldown_sec=0)
    worker.start()
    try:
        assert worker.submit(
            robot_id="robot-1",
            event_source="VISION_AI",
            event_type="ASSAULT",
        )
        assert worker.submit(
            robot_id="robot-1",
            event_source="SYSTEM_MONITOR",
            event_type="SYSTEM_ERROR",
        )
        worker.queue.join()
        assert len(database.events) == 2
        assert worker.thread is not None and worker.thread.is_alive()
    finally:
        worker.stop()


def test_status_writer_skips_unreceived_status_and_preserves_missing_values_as_null():
    database = RecordingDatabase()
    state = {"updated_at": None}
    writer = SystemStatusWriter(database, lambda: state)
    assert writer.write_once() is False
    state.update(
        {
            "updated_at": 1.0,
            "cpu_usage": "12.5",
            "cpu_temp": None,
            "ram_usage": "bad",
            "mode": "manual",
        }
    )
    assert writer.write_once() is True
    saved = database.statuses[0]
    assert saved["cpu_usage"] == 12.5
    assert saved["cpu_temperature"] is None
    assert saved["ram_usage"] is None
    assert saved["ping"] is None
    assert saved["is_autonomous"] == 0


def test_status_writer_stops_its_background_thread_cleanly():
    database = RecordingDatabase()
    writer = SystemStatusWriter(
        database,
        lambda: {"updated_at": 1.0, "mode": "auto"},
        interval_sec=0.1,
    )
    writer.start()
    time.sleep(0.15)
    writer.stop(timeout_sec=1)
    assert writer.thread is None
    assert database.statuses
