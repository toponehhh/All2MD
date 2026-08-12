from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .converter import ConversionBackend
from .errors import (
    ConversionBusyError,
    ConversionCancelledError,
    ConversionProcessError,
    ConversionTimeoutError,
    ResultTooLargeError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeResult:
    output_path: Path
    backend: ConversionBackend
    size_bytes: int


@dataclass(frozen=True)
class RuntimeProgress:
    sequence: int
    stage: str
    percent: int
    message: str
    backend: ConversionBackend | None
    attempt: int
    attempts: int

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RuntimeProgress:
        allowed_stages = {
            "preparing",
            "converting",
            "selecting_backend",
            "backend_failed",
            "backend_completed",
            "finalizing",
            "validating",
        }
        sequence = int(value["sequence"])
        percent = int(value["percent"])
        stage = str(value["stage"])
        message = str(value["message"])
        attempt = int(value.get("attempt", 0))
        attempts = int(value.get("attempts", 0))
        backend_value = value.get("backend")
        backend = ConversionBackend(str(backend_value)) if backend_value is not None else None
        if sequence < 1 or not 0 <= percent <= 99 or stage not in allowed_stages:
            raise ValueError("Invalid worker progress event")
        if attempt < 0 or attempts < 0 or (attempts and attempt > attempts):
            raise ValueError("Invalid worker backend attempt")
        if not message or len(message) > 200:
            raise ValueError("Invalid worker progress message")
        return cls(sequence, stage, percent, message, backend, attempt, attempts)


class ConversionRuntime:
    """Run each untrusted document conversion in a killable child process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._processes: set[asyncio.subprocess.Process] = set()
        self._capacity = asyncio.Semaphore(settings.conversion_concurrency)

    async def run(
        self,
        *,
        input_path: Path,
        output_path: Path,
        metadata_path: Path,
        backend: ConversionBackend,
        progress_path: Path | None = None,
        progress_callback: Callable[[RuntimeProgress], Awaitable[None]] | None = None,
        heartbeat_callback: Callable[[], Awaitable[None]] | None = None,
        cancellation_check: Callable[[], Awaitable[bool]] | None = None,
        acquire_timeout: float | None = None,
    ) -> RuntimeResult:
        try:
            if acquire_timeout is None:
                await self._capacity.acquire()
            else:
                await asyncio.wait_for(self._capacity.acquire(), timeout=acquire_timeout)
        except TimeoutError as exc:
            raise ConversionBusyError("All conversion workers are busy") from exc

        process: asyncio.subprocess.Process | None = None
        communicate: asyncio.Task[tuple[bytes, bytes]] | None = None
        worker_progress_path = progress_path or metadata_path.with_suffix(".progress.json")
        try:
            output_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            worker_progress_path.unlink(missing_ok=True)
            environment = os.environ.copy()
            # Untrusted document parsers do not need service authentication credentials.
            environment.pop("ALL2MD_API_KEY", None)
            environment.pop("ALL2MD_API_KEYS_JSON", None)
            environment["ALL2MD_WORKER_MEMORY_MB"] = str(self.settings.worker_memory_mb)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "all2md.worker",
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--metadata",
                str(metadata_path),
                "--progress",
                str(worker_progress_path),
                "--backend",
                backend.value,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            self._processes.add(process)
            communicate = asyncio.create_task(process.communicate())
            started = time.monotonic()
            last_heartbeat = started
            last_progress_sequence = 0

            async def consume_progress() -> None:
                nonlocal last_progress_sequence
                if progress_callback is None or not worker_progress_path.is_file():
                    return
                try:
                    raw_event = json.loads(worker_progress_path.read_text(encoding="utf-8"))
                    event = RuntimeProgress.from_dict(raw_event)
                except (AttributeError, OSError, ValueError, KeyError, TypeError):
                    return
                if event.sequence <= last_progress_sequence:
                    return
                last_progress_sequence = event.sequence
                try:
                    await progress_callback(event)
                except Exception:
                    logger.exception("Failed to persist conversion progress event")

            while not communicate.done():
                if time.monotonic() - started >= self.settings.conversion_timeout_seconds:
                    await self._terminate(process)
                    raise ConversionTimeoutError("Document conversion timed out")
                if cancellation_check is not None and await cancellation_check():
                    await self._terminate(process)
                    raise ConversionCancelledError("Document conversion was cancelled")
                await consume_progress()
                now = time.monotonic()
                if heartbeat_callback is not None and now - last_heartbeat >= 1.0:
                    try:
                        await heartbeat_callback()
                    except Exception:
                        logger.exception("Failed to persist conversion worker heartbeat")
                    last_heartbeat = now
                with suppress(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(communicate), timeout=0.25)

            _, stderr = await communicate
            await consume_progress()
            if process.returncode != 0:
                diagnostic = stderr.decode("utf-8", errors="replace")[-4000:].strip()
                logger.error("Conversion worker failed: %s", diagnostic or "no diagnostic")
                raise ConversionProcessError(
                    "The selected backends could not convert this document"
                )
            if not output_path.is_file() or not metadata_path.is_file():
                raise ConversionProcessError("Conversion worker returned an incomplete result")

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            size_bytes = output_path.stat().st_size
            if size_bytes > self.settings.max_result_bytes:
                output_path.unlink(missing_ok=True)
                raise ResultTooLargeError("Converted Markdown exceeds the configured result limit")
            return RuntimeResult(
                output_path=output_path,
                backend=ConversionBackend(metadata["backend"]),
                size_bytes=size_bytes,
            )
        finally:
            if process is not None and process.returncode is None:
                await self._terminate(process)
            if communicate is not None and not communicate.done():
                communicate.cancel()
            if process is not None:
                self._processes.discard(process)
            metadata_path.unlink(missing_ok=True)
            worker_progress_path.unlink(missing_ok=True)
            worker_progress_path.with_suffix(worker_progress_path.suffix + ".tmp").unlink(
                missing_ok=True
            )
            self._capacity.release()

    async def close(self) -> None:
        await asyncio.gather(
            *(self._terminate(process) for process in tuple(self._processes)),
            return_exceptions=True,
        )

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
