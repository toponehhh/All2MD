from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_OWNER_ID
from .errors import IdempotencyConflictError, QueueFullError

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    owner_id: str
    filename: str
    content_type: str | None
    requested_backend: str
    status: str
    progress: int
    message: str
    stage: str
    progress_accuracy: str
    current_backend: str | None
    backend_attempt: int
    backend_attempts: int
    progress_revision: int
    stage_started_at: float | None
    heartbeat_at: float | None
    size_bytes: int
    sha256: str
    input_path: str
    result_path: str | None
    result_backend: str | None
    error_code: str | None
    error_message: str | None
    cancel_requested: bool
    created_at: float
    updated_at: float
    expires_at: float
    lease_expires_at: float | None
    idempotency_key: str | None

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if self.stage_started_at is not None:
            end = time.time() if self.status not in TERMINAL_STATUSES else self.updated_at
            value["stage_elapsed_seconds"] = round(max(0.0, end - self.stage_started_at), 3)
        else:
            value["stage_elapsed_seconds"] = None
        for private_field in (
            "input_path",
            "result_path",
            "lease_expires_at",
            "idempotency_key",
            "owner_id",
        ):
            value.pop(private_field, None)
        return value


class JobStore:
    """SQLite-backed durable job state shared safely by API processes."""

    def __init__(self, database_path: Path, jobs_dir: Path) -> None:
        self.database_path = database_path
        self.jobs_dir = jobs_dir

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS conversion_jobs (
                    job_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL DEFAULT '{DEFAULT_OWNER_ID}',
                    filename TEXT NOT NULL,
                    content_type TEXT,
                    requested_backend TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress_accuracy TEXT NOT NULL DEFAULT 'milestone',
                    current_backend TEXT,
                    backend_attempt INTEGER NOT NULL DEFAULT 0,
                    backend_attempts INTEGER NOT NULL DEFAULT 0,
                    progress_revision INTEGER NOT NULL DEFAULT 0,
                    stage_started_at REAL,
                    heartbeat_at REAL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    input_path TEXT NOT NULL,
                    result_path TEXT,
                    result_backend TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    lease_expires_at REAL,
                    idempotency_key TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_jobs_status_created "
                "ON conversion_jobs(status, created_at)"
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(conversion_jobs)").fetchall()
            }
            migrations = {
                "owner_id": (f"TEXT NOT NULL DEFAULT '{DEFAULT_OWNER_ID}'"),
                "idempotency_key": "TEXT",
                "stage": "TEXT NOT NULL DEFAULT 'queued'",
                "progress_accuracy": "TEXT NOT NULL DEFAULT 'milestone'",
                "current_backend": "TEXT",
                "backend_attempt": "INTEGER NOT NULL DEFAULT 0",
                "backend_attempts": "INTEGER NOT NULL DEFAULT 0",
                "progress_revision": "INTEGER NOT NULL DEFAULT 0",
                "stage_started_at": "REAL",
                "heartbeat_at": "REAL",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE conversion_jobs ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                UPDATE conversion_jobs
                SET stage = CASE status
                    WHEN 'running' THEN 'converting'
                    WHEN 'completed' THEN 'completed'
                    WHEN 'failed' THEN 'failed'
                    WHEN 'cancelled' THEN 'cancelled'
                    ELSE 'queued'
                END
                WHERE stage = 'queued' AND status <> 'queued'
                """
            )
            connection.execute(
                """
                UPDATE conversion_jobs
                SET progress_accuracy = CASE
                        WHEN status = 'completed' THEN 'exact'
                        ELSE progress_accuracy
                    END,
                    stage_started_at = COALESCE(stage_started_at, updated_at)
                """
            )
            connection.execute("DROP INDEX IF EXISTS ux_jobs_idempotency")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_owner_idempotency "
                "ON conversion_jobs(owner_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rate_limits (
                    identity TEXT PRIMARY KEY,
                    window_started_at REAL NOT NULL,
                    request_count INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS service_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def check(self) -> None:
        with self._connect() as connection:
            result = connection.execute("PRAGMA quick_check(1)").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"SQLite quick check failed: {result}")
            connection.execute(
                """
                INSERT INTO service_meta(key, value) VALUES ('last_readiness_check', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(time.time()),),
            )

    def create(
        self,
        *,
        job_id: str,
        filename: str,
        content_type: str | None,
        requested_backend: str,
        size_bytes: int,
        sha256: str,
        input_path: Path,
        ttl_seconds: int,
        queue_capacity: int,
        owner_id: str = DEFAULT_OWNER_ID,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                existing = connection.execute(
                    """
                    SELECT job_id, filename, requested_backend, sha256
                    FROM conversion_jobs WHERE owner_id = ? AND idempotency_key = ?
                    """,
                    (owner_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["filename"] != filename
                        or existing["requested_backend"] != requested_backend
                        or existing["sha256"] != sha256
                    ):
                        raise IdempotencyConflictError(
                            "Idempotency-Key was already used for a different request"
                        )
                    connection.rollback()
                    record = self.get_for_owner(existing["job_id"], owner_id)
                    assert record is not None
                    return record
            active = connection.execute(
                "SELECT COUNT(*) FROM conversion_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
            if active >= queue_capacity:
                raise QueueFullError("The conversion queue is full")
            connection.execute(
                """
                INSERT INTO conversion_jobs (
                    job_id, owner_id, filename, content_type, requested_backend, status,
                    progress, message, stage, progress_accuracy, stage_started_at,
                    size_bytes, sha256, input_path, created_at, updated_at,
                    expires_at, idempotency_key
                ) VALUES (
                    ?, ?, ?, ?, ?, 'queued', 0, 'Queued conversion job', 'queued',
                    'milestone', ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    job_id,
                    owner_id,
                    filename,
                    content_type,
                    requested_backend,
                    now,
                    size_bytes,
                    sha256,
                    str(input_path),
                    now,
                    now,
                    now + ttl_seconds,
                    idempotency_key,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        record = self.get(job_id)
        assert record is not None
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversion_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def get_for_owner(self, job_id: str, owner_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversion_jobs WHERE job_id = ? AND owner_id = ?",
                (job_id, owner_id),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def claim_next(self, concurrency: int, lease_seconds: float) -> JobRecord | None:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                "SELECT COUNT(*) FROM conversion_jobs WHERE status = 'running'"
            ).fetchone()[0]
            if running >= concurrency:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT job_id FROM conversion_jobs "
                "WHERE status = 'queued' AND cancel_requested = 0 "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            job_id = row["job_id"]
            connection.execute(
                """
                UPDATE conversion_jobs
                SET status = 'running', progress = 5,
                    message = 'Starting isolated conversion process',
                    stage = 'preparing', progress_accuracy = 'milestone',
                    current_backend = NULL, backend_attempt = 0, backend_attempts = 0,
                    progress_revision = progress_revision + 1,
                    stage_started_at = ?, heartbeat_at = ?,
                    updated_at = ?, lease_expires_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (now, now, now, now + lease_seconds, job_id),
            )
            connection.commit()
        finally:
            connection.close()
        return self.get(job_id)

    def update_running(
        self,
        job_id: str,
        progress: int,
        message: str,
        *,
        stage: str,
        progress_accuracy: str = "milestone",
        current_backend: str | None = None,
        backend_attempt: int = 0,
        backend_attempts: int = 0,
    ) -> None:
        if not 0 <= progress <= 99:
            raise ValueError("Running progress must be between 0 and 99")
        if progress_accuracy not in {"milestone", "exact"}:
            raise ValueError("Unknown progress accuracy")
        if not stage or len(stage) > 64 or not message or len(message) > 200:
            raise ValueError("Invalid progress stage or message")
        if backend_attempt < 0 or backend_attempts < 0:
            raise ValueError("Backend attempts cannot be negative")
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversion_jobs
                SET progress = MAX(progress, ?), message = ?,
                    stage_started_at = CASE
                        WHEN stage <> ? OR stage_started_at IS NULL THEN ?
                        ELSE stage_started_at
                    END,
                    stage = ?, progress_accuracy = ?,
                    current_backend = COALESCE(?, current_backend),
                    backend_attempt = CASE WHEN ? > 0 THEN ? ELSE backend_attempt END,
                    backend_attempts = CASE WHEN ? > 0 THEN ? ELSE backend_attempts END,
                    progress_revision = progress_revision + 1,
                    heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (
                    progress,
                    message,
                    stage,
                    now,
                    stage,
                    progress_accuracy,
                    current_backend,
                    backend_attempt,
                    backend_attempt,
                    backend_attempts,
                    backend_attempts,
                    now,
                    now,
                    job_id,
                ),
            )

    def heartbeat(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE conversion_jobs SET heartbeat_at = ?, "
                "progress_revision = progress_revision + 1 "
                "WHERE job_id = ? AND status = 'running'",
                (time.time(), job_id),
            )

    def complete(self, job_id: str, result_path: Path, backend: str, ttl_seconds: int) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversion_jobs
                SET status = 'completed', progress = 100, message = 'Conversion completed',
                    stage = 'completed', progress_accuracy = 'exact',
                    current_backend = ?, progress_revision = progress_revision + 1,
                    stage_started_at = ?, heartbeat_at = ?,
                    result_path = ?, result_backend = ?, updated_at = ?, expires_at = ?,
                    lease_expires_at = NULL
                WHERE job_id = ? AND status = 'running'
                """,
                (
                    backend,
                    now,
                    now,
                    str(result_path),
                    backend,
                    now,
                    now + ttl_seconds,
                    job_id,
                ),
            )

    def fail(self, job_id: str, code: str, message: str, ttl_seconds: int) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversion_jobs
                SET status = 'failed', message = ?, stage = 'failed',
                    progress_accuracy = 'milestone', progress_revision = progress_revision + 1,
                    stage_started_at = ?, heartbeat_at = ?, error_code = ?,
                    error_message = ?, updated_at = ?, expires_at = ?, lease_expires_at = NULL
                WHERE job_id = ? AND status = 'running'
                """,
                (message, now, now, code, message, now, now + ttl_seconds, job_id),
            )

    def cancel(
        self,
        job_id: str,
        ttl_seconds: int,
        *,
        owner_id: str | None = None,
    ) -> JobRecord | None:
        now = time.time()
        owner_clause = " AND owner_id = ?" if owner_id is not None else ""
        parameters: tuple[object, ...] = (
            now,
            now,
            now,
            now + ttl_seconds,
            job_id,
            *((owner_id,) if owner_id is not None else ()),
        )
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE conversion_jobs
                SET cancel_requested = 1,
                    status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                    message = CASE WHEN status = 'queued' THEN 'Conversion cancelled'
                                   ELSE 'Cancellation requested' END,
                    stage = CASE WHEN status = 'queued' THEN 'cancelled' ELSE stage END,
                    progress_accuracy = 'milestone',
                    progress_revision = progress_revision + 1,
                    stage_started_at = CASE WHEN status = 'queued' THEN ? ELSE stage_started_at END,
                    heartbeat_at = CASE WHEN status = 'queued' THEN ? ELSE heartbeat_at END,
                    updated_at = ?,
                    expires_at = CASE WHEN status = 'queued' THEN ? ELSE expires_at END
                WHERE job_id = ? AND status IN ('queued', 'running'){owner_clause}
                """,
                parameters,
            )
        return self.get_for_owner(job_id, owner_id) if owner_id is not None else self.get(job_id)

    def mark_cancelled(self, job_id: str, ttl_seconds: int) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversion_jobs SET status = 'cancelled',
                    message = 'Conversion cancelled', stage = 'cancelled',
                    progress_accuracy = 'milestone', progress_revision = progress_revision + 1,
                    stage_started_at = ?, heartbeat_at = ?, updated_at = ?, expires_at = ?,
                    lease_expires_at = NULL
                WHERE job_id = ? AND status = 'running'
                """,
                (now, now, now, now + ttl_seconds, job_id),
            )

    def release(self, job_id: str) -> None:
        """Return an interrupted running job to the durable queue during shutdown."""
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE conversion_jobs
                SET status = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END,
                    progress = 0,
                    message = CASE WHEN cancel_requested = 1 THEN 'Conversion cancelled'
                                   ELSE 'Requeued during service shutdown' END,
                    stage = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END,
                    progress_accuracy = 'milestone',
                    current_backend = NULL, backend_attempt = 0, backend_attempts = 0,
                    progress_revision = progress_revision + 1,
                    stage_started_at = ?, heartbeat_at = NULL,
                    updated_at = ?, lease_expires_at = NULL
                WHERE job_id = ? AND status = 'running'
                """,
                (now, now, job_id),
            )

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM conversion_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return row is None or bool(row[0])

    def requeue_expired_leases(self) -> int:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversion_jobs
                SET status = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END,
                    progress = 0,
                    message = CASE WHEN cancel_requested = 1 THEN 'Conversion cancelled'
                                   ELSE 'Recovered after worker interruption' END,
                    stage = CASE WHEN cancel_requested = 1 THEN 'cancelled' ELSE 'queued' END,
                    progress_accuracy = 'milestone',
                    current_backend = NULL, backend_attempt = 0, backend_attempts = 0,
                    progress_revision = progress_revision + 1,
                    stage_started_at = ?, heartbeat_at = NULL,
                    updated_at = ?, lease_expires_at = NULL
                WHERE status = 'running' AND lease_expires_at < ?
                """,
                (now, now, now),
            )
            return cursor.rowcount

    def delete_expired(self) -> list[Path]:
        now = time.time()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM conversion_jobs "
                "WHERE status IN ('completed', 'failed', 'cancelled') AND expires_at < ?",
                (now,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM conversion_jobs WHERE job_id = ?",
                [(row["job_id"],) for row in rows],
            )
            connection.execute("DELETE FROM rate_limits WHERE window_started_at < ?", (now - 120,))
        return [self.jobs_dir / row["job_id"] for row in rows]

    def counts(self, owner_id: str | None = None) -> dict[str, int]:
        counts = {status: 0 for status in ("queued", "running", "completed", "failed", "cancelled")}
        with self._connect() as connection:
            if owner_id is None:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM conversion_jobs GROUP BY status"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM conversion_jobs "
                    "WHERE owner_id = ? GROUP BY status",
                    (owner_id,),
                ).fetchall()
        for row in rows:
            counts[row["status"]] = row["count"]
        return counts

    def consume_rate_limit(
        self, identity: str, limit: int, window_seconds: int = 60
    ) -> tuple[bool, int]:
        """Atomically consume one fixed-window request token."""
        if limit <= 0:
            return True, 0
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT window_started_at, request_count FROM rate_limits WHERE identity = ?",
                (identity,),
            ).fetchone()
            if row is None or row["window_started_at"] + window_seconds <= now:
                connection.execute(
                    """
                    INSERT INTO rate_limits(identity, window_started_at, request_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(identity) DO UPDATE SET
                        window_started_at = excluded.window_started_at,
                        request_count = 1
                    """,
                    (identity, now),
                )
                connection.commit()
                return True, 0
            retry_after = max(1, int(row["window_started_at"] + window_seconds - now) + 1)
            if row["request_count"] >= limit:
                connection.rollback()
                return False, retry_after
            connection.execute(
                "UPDATE rate_limits SET request_count = request_count + 1 WHERE identity = ?",
                (identity,),
            )
            connection.commit()
            return True, 0
        finally:
            connection.close()

    @staticmethod
    def _from_row(row: sqlite3.Row) -> JobRecord:
        values = dict(row)
        values["cancel_requested"] = bool(values["cancel_requested"])
        return JobRecord(**values)
