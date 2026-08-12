from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from all2md.config import DEFAULT_OWNER_ID, owner_id_for_tenant
from all2md.errors import QueueFullError
from all2md.job_store import JobStore


def create_job(
    store: JobStore,
    job_id: str,
    input_path: Path,
    capacity: int = 10,
    owner_id: str = DEFAULT_OWNER_ID,
    idempotency_key: str | None = None,
):
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(b"data")
    return store.create(
        job_id=job_id,
        filename="report.pdf",
        content_type="application/pdf",
        requested_backend="auto",
        size_bytes=4,
        sha256="0" * 64,
        input_path=input_path,
        ttl_seconds=3600,
        queue_capacity=capacity,
        owner_id=owner_id,
        idempotency_key=idempotency_key,
    )


def test_jobs_survive_store_recreation(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    store.initialize()
    create_job(store, "job-1", tmp_path / "jobs/job-1/input.pdf")

    reopened = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    reopened.initialize()

    assert reopened.get("job-1").status == "queued"


def test_queue_capacity_is_enforced_atomically(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    store.initialize()
    create_job(store, "job-1", tmp_path / "jobs/job-1/input.pdf", capacity=1)

    with pytest.raises(QueueFullError):
        create_job(store, "job-2", tmp_path / "jobs/job-2/input.pdf", capacity=1)


def test_expired_worker_lease_is_recovered(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    store.initialize()
    create_job(store, "job-1", tmp_path / "jobs/job-1/input.pdf")
    claimed = store.claim_next(concurrency=1, lease_seconds=30)
    assert claimed.status == "running"

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE conversion_jobs SET lease_expires_at = ? WHERE job_id = ?",
            (time.time() - 1, "job-1"),
        )

    assert store.requeue_expired_leases() == 1
    recovered = store.get("job-1")
    assert recovered.status == "queued"
    assert recovered.progress == 0
    assert recovered.stage == "queued"
    assert recovered.current_backend is None


def test_progress_is_monotonic_and_failure_does_not_report_completion(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    store.initialize()
    created = create_job(store, "job-1", tmp_path / "jobs/job-1/input.pdf")

    assert created.progress == 0
    assert created.stage == "queued"
    assert created.progress_accuracy == "milestone"

    claimed = store.claim_next(concurrency=1, lease_seconds=30)
    assert claimed.progress == 5
    assert claimed.stage == "preparing"

    store.update_running(
        "job-1",
        42,
        "Trying anydoc backend (1 of 3)",
        stage="converting",
        current_backend="anydoc",
        backend_attempt=1,
        backend_attempts=3,
    )
    store.update_running(
        "job-1",
        30,
        "Stale lower progress event",
        stage="converting",
        current_backend="anydoc",
        backend_attempt=1,
        backend_attempts=3,
    )
    running = store.get("job-1")

    assert running.progress == 42
    assert running.current_backend == "anydoc"
    assert (running.backend_attempt, running.backend_attempts) == (1, 3)
    assert running.progress_revision >= 3
    assert running.heartbeat_at is not None

    store.fail("job-1", "conversion_failed", "Conversion failed", ttl_seconds=3600)
    failed = store.get("job-1")

    assert failed.status == "failed"
    assert failed.stage == "failed"
    assert failed.progress == 42
    assert failed.progress_accuracy == "milestone"


def test_initialize_migrates_legacy_progress_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "jobs.sqlite3"
    now = time.time()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE conversion_jobs (
                job_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                content_type TEXT,
                requested_backend TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                message TEXT NOT NULL,
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
            """
            INSERT INTO conversion_jobs (
                job_id, filename, requested_backend, status, progress, message,
                size_bytes, sha256, input_path, created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "report.pdf",
                "auto",
                "completed",
                100,
                "Conversion completed",
                4,
                "0" * 64,
                str(tmp_path / "input.pdf"),
                now - 10,
                now,
                now + 3600,
            ),
        )

    store = JobStore(database_path, tmp_path / "jobs")
    store.initialize()
    migrated = store.get("legacy-job")

    assert migrated.stage == "completed"
    assert migrated.progress_accuracy == "exact"
    assert migrated.stage_started_at == pytest.approx(now)
    assert migrated.owner_id == DEFAULT_OWNER_ID


def test_job_ownership_and_idempotency_are_tenant_scoped(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3", tmp_path / "jobs")
    store.initialize()
    tenant_a = owner_id_for_tenant("tenant-a")
    tenant_b = owner_id_for_tenant("tenant-b")

    job_a = create_job(
        store,
        "job-a",
        tmp_path / "jobs/job-a/input.pdf",
        owner_id=tenant_a,
        idempotency_key="same-key",
    )
    job_b = create_job(
        store,
        "job-b",
        tmp_path / "jobs/job-b/input.pdf",
        owner_id=tenant_b,
        idempotency_key="same-key",
    )

    assert store.get_for_owner(job_a.job_id, tenant_a) == job_a
    assert store.get_for_owner(job_a.job_id, tenant_b) is None
    assert store.get_for_owner(job_b.job_id, tenant_b) == job_b
    assert store.counts(tenant_a)["queued"] == 1
    assert store.counts(tenant_b)["queued"] == 1

    assert store.cancel(job_a.job_id, 3600, owner_id=tenant_b) is None
    assert store.get_for_owner(job_a.job_id, tenant_a).status == "queued"
