"""Environment-configured MySQL access used by server services."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterator

from server.env_config import EnvConfigurationError, env_int, env_text

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ModuleNotFoundError:  # Keep non-auth server features bootable until dependencies are installed.
    pymysql = None
    DictCursor = None


class DatabaseConfigurationError(RuntimeError):
    """Raised when the server cannot safely connect to the configured database."""


class DatabaseOperationError(RuntimeError):
    """Raised when a configured database operation cannot be completed."""


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    connect_timeout_sec: int

    @classmethod
    def from_environment(cls) -> "DatabaseConfig":
        try:
            return cls(
                host=env_text("DB_HOST"),
                port=env_int("DB_PORT", minimum=1, maximum=65535),
                name=env_text("DB_NAME"),
                user=env_text("DB_USER"),
                password=env_text("DB_PASSWORD"),
                connect_timeout_sec=env_int(
                    "DB_CONNECT_TIMEOUT_SEC", minimum=1
                ),
            )
        except EnvConfigurationError as exc:
            raise DatabaseConfigurationError("Database configuration is invalid.") from exc

    def validate(self) -> None:
        if not all((self.host, self.name, self.user, self.password)):
            raise DatabaseConfigurationError(
                "Database configuration is incomplete. Set DB_HOST, DB_NAME, DB_USER, and DB_PASSWORD."
            )


class Database:
    def __init__(self, config: DatabaseConfig | None = None) -> None:
        self.config = config or DatabaseConfig.from_environment()

    def _connect(self):
        self.config.validate()
        if pymysql is None or DictCursor is None:
            raise DatabaseConfigurationError(
                "PyMySQL is unavailable. Install the project requirements before using authentication."
            )
        try:
            return pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.name,
                charset="utf8mb4",
                cursorclass=DictCursor,
                connect_timeout=self.config.connect_timeout_sec,
                read_timeout=self.config.connect_timeout_sec,
                write_timeout=self.config.connect_timeout_sec,
                autocommit=False,
            )
        except Exception as exc:
            raise DatabaseConfigurationError("Unable to connect to the configured database.") from exc

    @contextmanager
    def transaction(self) -> Iterator[object]:
        connection = self._connect()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def insert_system_status(self, values: dict[str, Any]) -> int:
        columns = (
            "cpu_usage",
            "cpu_temperature",
            "ram_usage",
            "ping",
            "is_autonomous",
            "speed",
            "gps_lat",
            "gps_lng",
            "gps_alt",
            "lidar_x",
            "lidar_y",
        )
        sql = f"""
            INSERT INTO system_status ({", ".join(columns)})
            VALUES ({", ".join(["%s"] * len(columns))})
        """
        return self._insert(sql, tuple(values.get(column) for column in columns))

    def insert_event(
        self,
        *,
        event_source: str,
        event_type: str,
        image_path: str | None = None,
        confidence: float | None = None,
        gps_lat: float | None = None,
        gps_lng: float | None = None,
        gps_alt: float | None = None,
        lidar_x: float | None = None,
        lidar_y: float | None = None,
    ) -> int:
        image_path = self._relative_image_path(image_path)
        return self._insert(
            """
            INSERT INTO event_log
                (event_source, event_type, image_path, confidence,
                 gps_lat, gps_lng, gps_alt, lidar_x, lidar_y)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_source,
                event_type,
                image_path,
                confidence,
                gps_lat,
                gps_lng,
                gps_alt,
                lidar_x,
                lidar_y,
            ),
        )

    def insert_action(
        self,
        *,
        user_id: int,
        action_type: str,
        event_id: int | None = None,
        description: str | None = None,
        image_path: str | None = None,
    ) -> int:
        image_path = self._relative_image_path(image_path)
        return self._insert(
            """
            INSERT INTO action_log
                (user_id, event_id, action_type, description_content, image_path)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user_id, event_id, action_type, description, image_path),
        )

    def mark_event_reported(self, event_id: int) -> bool:
        try:
            with self.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE event_log
                        SET is_reported = 1, reported_at = CURRENT_TIMESTAMP
                        WHERE event_id = %s AND is_deleted = 0
                        """,
                        (event_id,),
                    )
                    return cursor.rowcount == 1
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Unable to update the event log.") from exc

    def set_event_false_alarm(self, event_id: int, is_false_alarm: bool) -> bool:
        try:
            with self.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE event_log
                        SET is_false_alarm = %s
                        WHERE event_id = %s AND is_deleted = 0
                        """,
                        (int(is_false_alarm), event_id),
                    )
                    return cursor.rowcount == 1
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Unable to update the event log.") from exc

    def event_exists(self, event_id: int) -> bool:
        try:
            with self.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT 1 FROM event_log
                        WHERE event_id = %s AND is_deleted = 0
                        LIMIT 1
                        """,
                        (event_id,),
                    )
                    return cursor.fetchone() is not None
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Unable to read the event log.") from exc

    def list_system_status(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "recorded_at",
        sort_direction: str = "desc",
    ) -> dict[str, Any]:
        conditions, params = self._time_conditions(
            "recorded_at", start_at=start_at, end_at=end_at
        )
        where = self._where_clause(conditions)
        order_by, direction = self._sort_clause(
            sort_by,
            sort_direction,
            {
                "recorded_at": "recorded_at",
                "cpu_usage": "cpu_usage",
                "cpu_temperature": "cpu_temperature",
                "ram_usage": "ram_usage",
                "ping": "ping",
                "speed": "speed",
            },
        )
        return self._select_page(
            select_sql=f"""
                SELECT status_id, cpu_usage, cpu_temperature, ram_usage,
                       ping, is_autonomous, speed, gps_lat,
                       gps_lng, gps_alt, lidar_x, lidar_y, recorded_at
                FROM system_status{where}
            """,
            count_sql=f"SELECT COUNT(*) AS total FROM system_status{where}",
            params=tuple(params),
            order_by=order_by,
            tie_breaker="status_id",
            direction=direction,
            page=page,
            page_size=page_size,
        )

    def list_events(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        event_type: str | None = None,
        confidence_min: float | None = None,
        confidence_max: float | None = None,
        is_resolved: bool | None = None,
        is_reported: bool | None = None,
        is_alerted: bool | None = None,
        is_false_alarm: bool | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "recorded_at",
        sort_direction: str = "desc",
    ) -> dict[str, Any]:
        self._validate_confidence_range(confidence_min, confidence_max)
        conditions, params = self._time_conditions(
            "e.detected_at", start_at=start_at, end_at=end_at
        )
        conditions.insert(0, "e.is_deleted = 0")
        if event_type is not None:
            conditions.append("e.event_type = %s")
            params.append(event_type)
        if confidence_min is not None:
            conditions.append("e.confidence >= %s")
            params.append(confidence_min)
        if confidence_max is not None:
            conditions.append("e.confidence <= %s")
            params.append(confidence_max)
        for column, value in (
            ("is_resolved", is_resolved),
            ("is_reported", is_reported),
            ("is_alerted", is_alerted),
            ("is_false_alarm", is_false_alarm),
        ):
            if value is not None:
                conditions.append(f"e.{column} = %s")
                params.append(int(value))
        where = self._where_clause(conditions)
        order_by, direction = self._sort_clause(
            sort_by,
            sort_direction,
            {
                "recorded_at": "e.detected_at",
                "detected_at": "e.detected_at",
                "event_type": "e.event_type",
                "confidence": "e.confidence",
                "is_resolved": "e.is_resolved",
                "is_reported": "e.is_reported",
                "is_alerted": "e.is_alerted",
                "is_false_alarm": "e.is_false_alarm",
            },
        )
        return self._select_page(
            select_sql=f"""
                SELECT e.event_id, e.event_source, e.event_type,
                       CASE WHEN e.image_deleted_at IS NULL
                            THEN e.image_path ELSE NULL END AS image_path,
                       e.confidence, e.gps_lat, e.gps_lng, e.gps_alt,
                       e.lidar_x, e.lidar_y, e.is_resolved, e.is_reported,
                       e.reported_at, e.is_alerted, e.is_mic_used,
                       e.is_false_alarm, e.detected_at
                FROM event_log AS e{where}
            """,
            count_sql=f"SELECT COUNT(*) AS total FROM event_log AS e{where}",
            params=tuple(params),
            order_by=order_by,
            tie_breaker="e.event_id",
            direction=direction,
            page=page,
            page_size=page_size,
        )

    def list_actions(
        self,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        user_name: str | None = None,
        action_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "recorded_at",
        sort_direction: str = "desc",
    ) -> dict[str, Any]:
        conditions, params = self._time_conditions(
            "a.created_at", start_at=start_at, end_at=end_at
        )
        conditions.insert(0, "a.is_deleted = 0")
        normalized_user_name = (user_name or "").strip()
        if normalized_user_name:
            conditions.append("INSTR(u.name, %s) > 0")
            params.append(normalized_user_name)
        if action_type is not None:
            conditions.append("a.action_type = %s")
            params.append(action_type)
        where = self._where_clause(conditions)
        order_by, direction = self._sort_clause(
            sort_by,
            sort_direction,
            {
                "recorded_at": "a.created_at",
                "created_at": "a.created_at",
                "user_name": "u.name",
                "action_type": "a.action_type",
                "event_id": "a.event_id",
            },
        )
        joins = """
            FROM action_log AS a
            INNER JOIN users AS u ON u.user_id = a.user_id
        """
        return self._select_page(
            select_sql=f"""
                SELECT a.action_id, a.user_id, u.name AS user_name,
                       u.email AS user_email, a.event_id, a.action_type,
                       a.description_content,
                       CASE WHEN a.image_deleted_at IS NULL
                            THEN a.image_path ELSE NULL END AS image_path,
                       a.created_at
                {joins}{where}
            """,
            count_sql=f"SELECT COUNT(*) AS total {joins}{where}",
            params=tuple(params),
            order_by=order_by,
            tie_breaker="a.action_id",
            direction=direction,
            page=page,
            page_size=page_size,
        )

    def list_gallery(
        self,
        *,
        source: str = "all",
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        normalized_source = source.strip().lower()
        if normalized_source not in {"all", "event", "action"}:
            raise ValueError("source must be one of: all, event, action")

        selects: list[str] = []
        params: list[Any] = []
        if normalized_source in {"all", "event"}:
            conditions = [
                "e.is_deleted = 0",
                "e.image_path IS NOT NULL",
                "e.image_deleted_at IS NULL",
            ]
            if start_at is not None:
                conditions.append("e.detected_at >= %s")
                params.append(start_at)
            if end_at is not None:
                conditions.append("e.detected_at <= %s")
                params.append(end_at)
            selects.append(
                """
                SELECT 'event' AS source, e.event_id AS record_id,
                       e.event_id, NULL AS action_id, NULL AS user_id,
                       NULL AS user_name, e.event_source, e.event_type,
                       NULL AS action_type, NULL AS description_content,
                       e.image_path, e.confidence, e.gps_lat, e.gps_lng,
                       e.gps_alt, e.lidar_x, e.lidar_y,
                       e.detected_at AS recorded_at
                FROM event_log AS e
                WHERE """
                + " AND ".join(conditions)
            )
        if normalized_source in {"all", "action"}:
            conditions = [
                "a.is_deleted = 0",
                "a.image_path IS NOT NULL",
                "a.image_deleted_at IS NULL",
            ]
            if start_at is not None:
                conditions.append("a.created_at >= %s")
                params.append(start_at)
            if end_at is not None:
                conditions.append("a.created_at <= %s")
                params.append(end_at)
            selects.append(
                """
                SELECT 'action' AS source, a.action_id AS record_id,
                       a.event_id, a.action_id, a.user_id, u.name AS user_name,
                       e.event_source, e.event_type, a.action_type,
                       a.description_content, a.image_path, e.confidence,
                       e.gps_lat, e.gps_lng, e.gps_alt, e.lidar_x, e.lidar_y,
                       a.created_at AS recorded_at
                FROM action_log AS a
                INNER JOIN users AS u ON u.user_id = a.user_id
                LEFT JOIN event_log AS e
                    ON e.event_id = a.event_id AND e.is_deleted = 0
                WHERE """
                + " AND ".join(conditions)
            )
        params.append(limit)
        return self._select(
            " UNION ALL ".join(selects)
            + " ORDER BY recorded_at DESC LIMIT %s",
            tuple(params),
        )

    def get_event_image_path(self, event_id: int) -> str | None:
        return self._get_image_path("event_log", "event_id", event_id)

    def get_action_image_path(self, action_id: int) -> str | None:
        return self._get_image_path("action_log", "action_id", action_id)

    def soft_delete_event_image(self, event_id: int) -> bool:
        return self._soft_delete_image("event_log", "event_id", event_id)

    def soft_delete_action_image(self, action_id: int) -> bool:
        return self._soft_delete_image("action_log", "action_id", action_id)

    def preview_log_soft_delete(
        self, *, event_ids: list[int], action_ids: list[int]
    ) -> dict[str, int]:
        return self._log_soft_delete_counts(
            event_ids=event_ids,
            action_ids=action_ids,
            apply_delete=False,
        )

    def soft_delete_logs(
        self, *, event_ids: list[int], action_ids: list[int]
    ) -> dict[str, int]:
        return self._log_soft_delete_counts(
            event_ids=event_ids,
            action_ids=action_ids,
            apply_delete=True,
        )

    def _insert(self, sql: str, params: tuple[Any, ...]) -> int:
        try:
            with self.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    return int(cursor.lastrowid)
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Unable to write to the configured database.") from exc

    @staticmethod
    def _time_conditions(
        timestamp_column: str,
        *,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> tuple[list[str], list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if start_at is not None:
            conditions.append(f"{timestamp_column} >= %s")
            params.append(start_at)
        if end_at is not None:
            conditions.append(f"{timestamp_column} <= %s")
            params.append(end_at)
        return conditions, params

    @staticmethod
    def _where_clause(conditions: list[str]) -> str:
        return f" WHERE {' AND '.join(conditions)}" if conditions else ""

    @staticmethod
    def _sort_clause(
        sort_by: str,
        sort_direction: str,
        allowed_columns: dict[str, str],
    ) -> tuple[str, str]:
        normalized_sort = str(sort_by or "").strip().lower()
        if normalized_sort not in allowed_columns:
            raise ValueError(f"Unsupported sort column: {sort_by}")
        normalized_direction = str(sort_direction or "").strip().lower()
        if normalized_direction not in {"asc", "desc"}:
            raise ValueError("sort_direction must be 'asc' or 'desc'.")
        return allowed_columns[normalized_sort], normalized_direction.upper()

    @staticmethod
    def _validate_confidence_range(
        confidence_min: float | None, confidence_max: float | None
    ) -> None:
        for name, value in (
            ("confidence_min", confidence_min),
            ("confidence_max", confidence_max),
        ):
            if value is not None and not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1.")
        if (
            confidence_min is not None
            and confidence_max is not None
            and confidence_min > confidence_max
        ):
            raise ValueError("confidence_min must not exceed confidence_max.")

    def _select_page(
        self,
        *,
        select_sql: str,
        count_sql: str,
        params: tuple[Any, ...],
        order_by: str,
        tie_breaker: str,
        direction: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page must be a positive integer.")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 500
        ):
            raise ValueError("page_size must be between 1 and 500.")
        offset = (page - 1) * page_size
        paged_sql = (
            f"{select_sql} ORDER BY {order_by} {direction}, "
            f"{tie_breaker} {direction} LIMIT %s OFFSET %s"
        )
        try:
            with self.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(count_sql, params)
                    count_row = cursor.fetchone() or {}
                    total = int(count_row.get("total") or 0)
                    cursor.execute(paged_sql, params + (page_size, offset))
                    items = list(cursor.fetchall())
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Unable to read from the configured database.") from exc
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def _select(
        self, sql: str, params: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        try:
            with self.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    return list(cursor.fetchall())
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Unable to read from the configured database.") from exc

    def _get_image_path(
        self, table: str, id_column: str, record_id: int
    ) -> str | None:
        # Identifiers are fixed by the two public callers above.
        rows = self._select(
            f"""
            SELECT image_path FROM {table}
            WHERE {id_column} = %s
              AND is_deleted = 0
              AND image_deleted_at IS NULL
            LIMIT 1
            """,
            (record_id,),
        )
        if not rows or rows[0].get("image_path") is None:
            return None
        return str(rows[0]["image_path"])

    def _soft_delete_image(
        self, table: str, id_column: str, record_id: int
    ) -> bool:
        # Identifiers are fixed by the two public callers above. Soft deletion
        # intentionally preserves the log row, image_path, and JPEG file.
        try:
            with self.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE {table}
                        SET image_deleted_at = CURRENT_TIMESTAMP
                        WHERE {id_column} = %s
                          AND is_deleted = 0
                          AND image_path IS NOT NULL
                          AND image_deleted_at IS NULL
                        """,
                        (record_id,),
                    )
                    return cursor.rowcount == 1
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseOperationError("Unable to soft-delete the image.") from exc

    def _log_soft_delete_counts(
        self,
        *,
        event_ids: list[int],
        action_ids: list[int],
        apply_delete: bool,
    ) -> dict[str, int]:
        normalized_event_ids = self._validated_log_ids("event_ids", event_ids)
        normalized_action_ids = self._validated_log_ids("action_ids", action_ids)
        if not normalized_event_ids and not normalized_action_ids:
            raise ValueError("At least one event_id or action_id is required.")

        event_rows: list[dict[str, Any]] = []
        action_rows: list[dict[str, Any]] = []
        lock_clause = " FOR UPDATE" if apply_delete else ""
        try:
            with self.transaction() as connection:
                with connection.cursor() as cursor:
                    if normalized_event_ids:
                        placeholders = ", ".join(["%s"] * len(normalized_event_ids))
                        cursor.execute(
                            f"""
                            SELECT event_id, image_path, image_deleted_at
                            FROM event_log
                            WHERE is_deleted = 0
                              AND event_id IN ({placeholders}){lock_clause}
                            """,
                            normalized_event_ids,
                        )
                        event_rows = list(cursor.fetchall())

                    active_event_ids = tuple(
                        int(row["event_id"]) for row in event_rows
                    )
                    action_conditions: list[str] = []
                    action_params: list[int] = []
                    if normalized_action_ids:
                        placeholders = ", ".join(["%s"] * len(normalized_action_ids))
                        action_conditions.append(f"action_id IN ({placeholders})")
                        action_params.extend(normalized_action_ids)
                    if active_event_ids:
                        placeholders = ", ".join(["%s"] * len(active_event_ids))
                        action_conditions.append(f"event_id IN ({placeholders})")
                        action_params.extend(active_event_ids)
                    if action_conditions:
                        cursor.execute(
                            f"""
                            SELECT action_id, event_id, image_path, image_deleted_at
                            FROM action_log
                            WHERE is_deleted = 0
                              AND ({" OR ".join(action_conditions)}){lock_clause}
                            """,
                            tuple(action_params),
                        )
                        action_rows = list(cursor.fetchall())

                    if apply_delete and action_rows:
                        affected_action_ids = tuple(
                            int(row["action_id"]) for row in action_rows
                        )
                        placeholders = ", ".join(["%s"] * len(affected_action_ids))
                        cursor.execute(
                            f"""
                            UPDATE action_log
                            SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP
                            WHERE is_deleted = 0
                              AND action_id IN ({placeholders})
                            """,
                            affected_action_ids,
                        )
                    if apply_delete and event_rows:
                        affected_event_ids = tuple(
                            int(row["event_id"]) for row in event_rows
                        )
                        placeholders = ", ".join(["%s"] * len(affected_event_ids))
                        cursor.execute(
                            f"""
                            UPDATE event_log
                            SET is_deleted = 1, deleted_at = CURRENT_TIMESTAMP
                            WHERE is_deleted = 0
                              AND event_id IN ({placeholders})
                            """,
                            affected_event_ids,
                        )
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            operation = "soft-delete" if apply_delete else "preview soft-deletion of"
            raise DatabaseOperationError(
                f"Unable to {operation} the selected logs."
            ) from exc

        newly_hidden_images = sum(
            row.get("image_path") is not None and row.get("image_deleted_at") is None
            for row in event_rows + action_rows
        )
        return {
            "events": len(event_rows),
            "actions": len(action_rows),
            "images": newly_hidden_images,
        }

    @staticmethod
    def _validated_log_ids(name: str, values: list[int]) -> tuple[int, ...]:
        if not isinstance(values, list):
            raise ValueError(f"{name} must be an array.")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError(f"{name} must contain only positive integers.")
        if len(set(values)) != len(values):
            raise ValueError(f"{name} must contain unique IDs.")
        return tuple(values)

    @staticmethod
    def _relative_image_path(image_path: str | None) -> str | None:
        if image_path is None:
            return None
        normalized = image_path.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or PureWindowsPath(image_path).is_absolute()
            or ".." in path.parts
        ):
            raise ValueError("image_path must be a non-empty relative path")
        return normalized
