from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import Counter


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("request_id", "method", "path", "status_code", "duration_ms"):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(json_output: bool) -> None:
    application_logger = logging.getLogger("all2md")
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False
    if application_logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    application_logger.addHandler(handler)


class Metrics:
    """Small dependency-free Prometheus collector for service-level metrics."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str | None, str, str, int]] = Counter()
        self._duration_sum: Counter[tuple[str | None, str, str]] = Counter()

    def observe_request(
        self,
        owner_id: str | None,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        with self._lock:
            self._requests[(owner_id, method, route, status_code)] += 1
            self._duration_sum[(owner_id, method, route)] += duration_seconds

    def render(self, job_counts: dict[str, int], owner_id: str) -> str:
        lines = [
            "# HELP all2md_uptime_seconds Process uptime in seconds.",
            "# TYPE all2md_uptime_seconds gauge",
            f"all2md_uptime_seconds {time.time() - self.started_at:.3f}",
            "# HELP all2md_http_requests_total HTTP requests handled by this process.",
            "# TYPE all2md_http_requests_total counter",
        ]
        with self._lock:
            requests = dict(self._requests)
            durations = dict(self._duration_sum)
        tenant_requests = ((key, count) for key, count in requests.items() if key[0] == owner_id)
        for (_, method, route, status), count in sorted(tenant_requests):
            labels = f'method="{method}",route="{route}",status="{status}"'
            lines.append(f"all2md_http_requests_total{{{labels}}} {count}")
        lines.extend(
            [
                "# HELP all2md_http_request_duration_seconds_sum Cumulative request duration.",
                "# TYPE all2md_http_request_duration_seconds_sum counter",
            ]
        )
        tenant_durations = (
            (key, duration) for key, duration in durations.items() if key[0] == owner_id
        )
        for (_, method, route), duration in sorted(tenant_durations):
            labels = f'method="{method}",route="{route}"'
            lines.append(f"all2md_http_request_duration_seconds_sum{{{labels}}} {duration:.6f}")
        lines.extend(
            [
                "# HELP all2md_jobs Current persisted jobs by status.",
                "# TYPE all2md_jobs gauge",
            ]
        )
        for status, count in sorted(job_counts.items()):
            lines.append(f'all2md_jobs{{status="{status}"}} {count}')
        return "\n".join(lines) + "\n"
