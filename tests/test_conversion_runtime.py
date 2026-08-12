from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from all2md.config import Settings
from all2md.conversion_runtime import ConversionRuntime, RuntimeProgress
from all2md.converter import ConversionBackend
from all2md.errors import ConversionTimeoutError, ResultTooLargeError


def runtime_settings(tmp_path: Path, **changes) -> Settings:
    settings = replace(
        Settings.from_env(),
        data_dir=tmp_path,
        conversion_timeout_seconds=10,
        max_result_bytes=1024,
        log_json=False,
    )
    return replace(settings, **changes)


def test_timeout_terminates_isolated_process(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input.csv"
    source.write_bytes(b"name,score\nAlice,10\n")
    runtime = ConversionRuntime(runtime_settings(tmp_path, conversion_timeout_seconds=0.001))

    class HangingProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.finished = asyncio.Event()

        async def communicate(self):
            await self.finished.wait()
            return b"", b""

        def terminate(self) -> None:
            self.returncode = -15
            self.finished.set()

        def kill(self) -> None:
            self.returncode = -9
            self.finished.set()

        async def wait(self):
            await self.finished.wait()
            return self.returncode

    hanging_process = HangingProcess()

    async def create_hanging_process(*args, **kwargs):
        return hanging_process

    monkeypatch.setattr(
        "all2md.conversion_runtime.asyncio.create_subprocess_exec",
        create_hanging_process,
    )

    async def run() -> None:
        with pytest.raises(ConversionTimeoutError):
            await runtime.run(
                input_path=source,
                output_path=tmp_path / "result.md",
                metadata_path=tmp_path / "result.json",
                backend=ConversionBackend.ANYDOC,
            )

    asyncio.run(run())

    assert not runtime._processes
    assert hanging_process.returncode == -15
    assert not (tmp_path / "result.json").exists()


def test_result_limit_removes_oversized_output(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_bytes(b"name,score\nAlice,10\n")
    runtime = ConversionRuntime(runtime_settings(tmp_path, max_result_bytes=1))

    async def run() -> None:
        with pytest.raises(ResultTooLargeError):
            await runtime.run(
                input_path=source,
                output_path=tmp_path / "result.md",
                metadata_path=tmp_path / "result.json",
                backend=ConversionBackend.ANYDOC,
            )

    asyncio.run(run())

    assert not (tmp_path / "result.md").exists()


def test_runtime_forwards_atomic_worker_progress_events(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_bytes(b"name,score\nAlice,10\n")
    output = tmp_path / "result.md"
    metadata = tmp_path / "result.json"
    progress_path = tmp_path / "progress.json"
    runtime = ConversionRuntime(runtime_settings(tmp_path))
    events: list[RuntimeProgress] = []

    async def capture_progress(event: RuntimeProgress) -> None:
        events.append(event)

    async def run() -> None:
        result = await runtime.run(
            input_path=source,
            output_path=output,
            metadata_path=metadata,
            progress_path=progress_path,
            backend=ConversionBackend.ANYDOC,
            progress_callback=capture_progress,
        )
        assert result.backend is ConversionBackend.ANYDOC

    asyncio.run(run())

    assert output.is_file()
    assert events
    assert [event.sequence for event in events] == sorted({event.sequence for event in events})
    assert [event.percent for event in events] == sorted(event.percent for event in events)
    assert events[-1].stage == "validating"
    assert events[-1].percent == 95
    assert events[-1].backend is ConversionBackend.ANYDOC
    assert (events[-1].attempt, events[-1].attempts) == (1, 1)
    assert not progress_path.exists()


def test_runtime_does_not_forward_api_credentials_to_document_parser(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "input.md"
    output = tmp_path / "result.md"
    metadata = tmp_path / "result.json"
    source.write_text("# Input", encoding="utf-8")
    runtime = ConversionRuntime(runtime_settings(tmp_path))
    monkeypatch.setenv("ALL2MD_API_KEY", "legacy-service-api-secret")
    monkeypatch.setenv(
        "ALL2MD_API_KEYS_JSON",
        '{"tenant-a":"tenant-service-api-secret"}',
    )
    captured_environment: dict[str, str] = {}

    class CompletedProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def create_completed_process(*args, **kwargs):
        captured_environment.update(kwargs["env"])
        output.write_text("# Output", encoding="utf-8")
        metadata.write_text(json.dumps({"backend": "direct"}), encoding="utf-8")
        return CompletedProcess()

    monkeypatch.setattr(
        "all2md.conversion_runtime.asyncio.create_subprocess_exec",
        create_completed_process,
    )

    asyncio.run(
        runtime.run(
            input_path=source,
            output_path=output,
            metadata_path=metadata,
            backend=ConversionBackend.DIRECT,
        )
    )

    assert "ALL2MD_API_KEY" not in captured_environment
    assert "ALL2MD_API_KEYS_JSON" not in captured_environment
    assert captured_environment["ALL2MD_WORKER_MEMORY_MB"] == str(runtime.settings.worker_memory_mb)
