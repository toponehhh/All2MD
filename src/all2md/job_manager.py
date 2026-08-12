from __future__ import annotations

import asyncio
import logging
import shutil
import time
from contextlib import suppress
from pathlib import Path

from .config import Settings
from .conversion_runtime import ConversionRuntime
from .converter import ConversionBackend
from .errors import (
    ConversionCancelledError,
    ConversionProcessError,
    ConversionTimeoutError,
    ResultTooLargeError,
)
from .job_store import TERMINAL_STATUSES, JobRecord, JobStore

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self, settings: Settings, store: JobStore, runtime: ConversionRuntime) -> None:
        self.settings = settings
        self.store = store
        self.runtime = runtime
        self._tasks: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()

    async def start(self) -> None:
        self.store.initialize()
        recovered = self.store.requeue_expired_leases()
        if recovered:
            logger.warning("Recovered %d jobs with expired worker leases", recovered)
        self._tasks = [
            *[
                asyncio.create_task(
                    self._worker_loop(), name=f"all2md-job-worker-{worker_number + 1}"
                )
                for worker_number in range(self.settings.conversion_concurrency)
            ],
            asyncio.create_task(self._cleanup_loop(), name="all2md-job-cleanup"),
        ]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.runtime.close()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    def notify(self) -> None:
        self._wake.set()

    async def _worker_loop(self) -> None:
        lease_seconds = self.settings.conversion_timeout_seconds + 30
        while True:
            job = await asyncio.to_thread(
                self.store.claim_next,
                self.settings.conversion_concurrency,
                lease_seconds,
            )
            if job is None:
                self._wake.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self.settings.worker_poll_seconds
                    )
                continue
            await self._process(job)

    async def _process(self, job: JobRecord) -> None:
        job_dir = self.store.jobs_dir / job.job_id
        output_path = job_dir / "result.md"
        metadata_path = job_dir / "result.json"
        progress_path = job_dir / "progress.json"
        await asyncio.to_thread(
            self.store.update_running,
            job.job_id,
            8,
            "Launching isolated conversion process",
            stage="preparing",
        )
        try:
            result = await self.runtime.run(
                input_path=Path(job.input_path),
                output_path=output_path,
                metadata_path=metadata_path,
                progress_path=progress_path,
                backend=ConversionBackend(job.requested_backend),
                progress_callback=lambda event: asyncio.to_thread(
                    self.store.update_running,
                    job.job_id,
                    event.percent,
                    event.message,
                    stage=event.stage,
                    current_backend=event.backend.value if event.backend else None,
                    backend_attempt=event.attempt,
                    backend_attempts=event.attempts,
                ),
                heartbeat_callback=lambda: asyncio.to_thread(self.store.heartbeat, job.job_id),
                cancellation_check=lambda: asyncio.to_thread(
                    self.store.is_cancel_requested, job.job_id
                ),
            )
            if await asyncio.to_thread(self.store.is_cancel_requested, job.job_id):
                result.output_path.unlink(missing_ok=True)
                await asyncio.to_thread(
                    self.store.mark_cancelled,
                    job.job_id,
                    self.settings.job_ttl_seconds,
                )
            else:
                await asyncio.to_thread(
                    self.store.update_running,
                    job.job_id,
                    98,
                    "Persisting conversion result",
                    stage="finalizing",
                    current_backend=result.backend.value,
                )
                await asyncio.to_thread(
                    self.store.complete,
                    job.job_id,
                    result.output_path,
                    result.backend.value,
                    self.settings.job_ttl_seconds,
                )
        except ConversionCancelledError:
            output_path.unlink(missing_ok=True)
            await asyncio.to_thread(
                self.store.mark_cancelled, job.job_id, self.settings.job_ttl_seconds
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.release, job.job_id)
            raise
        except ConversionTimeoutError:
            await asyncio.to_thread(
                self.store.fail,
                job.job_id,
                "conversion_timeout",
                "Document conversion exceeded the configured time limit",
                self.settings.job_ttl_seconds,
            )
        except ResultTooLargeError:
            await asyncio.to_thread(
                self.store.fail,
                job.job_id,
                "result_too_large",
                "Converted Markdown exceeded the configured size limit",
                self.settings.job_ttl_seconds,
            )
        except ConversionProcessError:
            await asyncio.to_thread(
                self.store.fail,
                job.job_id,
                "conversion_failed",
                "The document could not be converted by the selected backend",
                self.settings.job_ttl_seconds,
            )
        except Exception:
            logger.exception("Unexpected conversion failure for job %s", job.job_id)
            await asyncio.to_thread(
                self.store.fail,
                job.job_id,
                "internal_error",
                "An internal conversion error occurred",
                self.settings.job_ttl_seconds,
            )
        finally:
            current = await asyncio.to_thread(self.store.get, job.job_id)
            if current is not None and current.status in TERMINAL_STATUSES:
                Path(job.input_path).unlink(missing_ok=True)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.cleanup_interval_seconds)
            await asyncio.to_thread(self.store.requeue_expired_leases)
            expired_paths = await asyncio.to_thread(self.store.delete_expired)
            for path in expired_paths:
                await asyncio.to_thread(shutil.rmtree, path, True)
            await asyncio.to_thread(self._cleanup_stale_temp_directories)

    def _cleanup_stale_temp_directories(self) -> None:
        cutoff = time.time() - max(self.settings.conversion_timeout_seconds * 2, 600)
        if not self.settings.temp_dir.exists():
            return
        for path in self.settings.temp_dir.iterdir():
            with suppress(OSError):
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
