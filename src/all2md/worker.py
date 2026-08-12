from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .converter import ConversionBackend, ConversionProgress, convert_file_with_backend


def _apply_resource_limits() -> None:
    """Apply optional hard limits inside the isolated conversion process."""
    if os.name == "nt":
        return
    memory_mb = int(os.getenv("ALL2MD_WORKER_MEMORY_MB", "0"))
    if memory_mb <= 0:
        return
    import resource

    memory_bytes = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Internal isolated All2MD converter")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in ConversionBackend],
        default=ConversionBackend.AUTO.value,
    )
    arguments = parser.parse_args()
    _apply_resource_limits()
    sequence = 0
    last_backend: ConversionBackend | None = None
    last_attempt = 0
    last_attempts = 0

    def publish_progress(event: ConversionProgress) -> None:
        nonlocal last_attempt, last_attempts, last_backend, sequence
        sequence += 1
        if event.backend is not None:
            last_backend = event.backend
        if event.attempt > 0:
            last_attempt = event.attempt
        if event.attempts > 0:
            last_attempts = event.attempts
        _write_json_atomic(
            arguments.progress,
            {
                "sequence": sequence,
                "stage": event.stage,
                "percent": event.percent,
                "message": event.message,
                "backend": last_backend.value if last_backend is not None else None,
                "attempt": last_attempt,
                "attempts": last_attempts,
            },
        )

    try:
        publish_progress(
            ConversionProgress(
                stage="preparing",
                percent=10,
                message="Isolated conversion process started",
            )
        )
        result = convert_file_with_backend(
            arguments.input,
            arguments.output,
            ConversionBackend(arguments.backend),
            progress_callback=publish_progress,
        )
        publish_progress(
            ConversionProgress(
                stage="validating",
                percent=95,
                message="Validating converted Markdown",
                backend=result.backend,
            )
        )
        _write_json_atomic(
            arguments.metadata,
            {"backend": result.backend.value, "result_bytes": arguments.output.stat().st_size},
        )
        return 0
    except Exception as exc:
        # Detailed diagnostics stay in server logs and are never returned verbatim by the API.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
