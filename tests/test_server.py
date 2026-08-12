from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from all2md.config import Settings
from all2md.conversion_runtime import RuntimeProgress, RuntimeResult
from all2md.converter import ConversionBackend
from all2md.errors import ConversionCancelledError, ConversionProcessError
from all2md.server import create_app


def settings_for(tmp_path, **changes) -> Settings:
    defaults = replace(
        Settings.from_env(),
        data_dir=tmp_path,
        log_json=False,
        conversion_timeout_seconds=5,
        worker_poll_seconds=0.02,
        cleanup_interval_seconds=60,
    )
    return replace(defaults, **changes)


def install_fake_runtime(monkeypatch, content="# Converted", backend="anydoc") -> None:
    async def fake_run(self, **arguments):
        output_path = arguments["output_path"]
        output_path.write_text(content, encoding="utf-8")
        return RuntimeResult(
            output_path=output_path,
            backend=ConversionBackend(backend),
            size_bytes=len(content.encode("utf-8")),
        )

    monkeypatch.setattr("all2md.conversion_runtime.ConversionRuntime.run", fake_run)


def parse_sse_events(lines) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    current: dict[str, object] = {}
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if current or data_lines:
                if data_lines:
                    current["data"] = json.loads("\n".join(data_lines))
                events.append(current)
                current = {}
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.lstrip()
        if field == "data":
            data_lines.append(value)
        elif field in {"id", "retry"}:
            current[field] = int(value)
        else:
            current[field] = value
    if current or data_lines:
        if data_lines:
            current["data"] = json.loads("\n".join(data_lines))
        events.append(current)
    return events


def test_health_ready_and_backends(tmp_path) -> None:
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").json() == {"status": "ready"}
        response = client.get("/backends")

    names = {backend["name"] for backend in response.json()["backends"]}
    assert {"anydoc", "liteparse"}.issubset(names)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Request-ID"]


def test_api_key_protects_conversion_endpoints_but_not_health(tmp_path) -> None:
    app = create_app(settings_for(tmp_path, api_key="a-production-key-with-entropy"))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        unauthorized = client.get("/backends")
        authorized = client.get("/backends", headers={"X-API-Key": "a-production-key-with-entropy"})

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "invalid_api_key"
    assert authorized.status_code == 200


def test_background_parameter_returns_tenant_session(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch, content="# Background")
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/convert?background=true&backend=anydoc",
            files={"file": ("report.docx", b"document bytes")},
        )

    payload = response.json()
    assert response.status_code == 202
    assert payload["session_id"] == payload["job_id"]
    assert payload["status_url"] == f"/convert/sessions/{payload['session_id']}"
    assert payload["events_url"] == f"/convert/sessions/{payload['session_id']}/events"
    assert "owner_id" not in payload


def test_api_key_tenants_isolate_status_events_result_cancel_and_metrics(
    tmp_path, monkeypatch
) -> None:
    install_fake_runtime(monkeypatch, content="# Tenant A")
    key_a = "tenant-a-production-api-key"
    key_b = "tenant-b-production-api-key"
    app = create_app(
        settings_for(
            tmp_path,
            api_key=None,
            api_keys=(("tenant-a", key_a), ("tenant-b", key_b)),
        )
    )
    headers_a = {"X-API-Key": key_a, "Idempotency-Key": "same-operation"}
    headers_b = {"X-API-Key": key_b, "Idempotency-Key": "same-operation"}

    with TestClient(app) as client:
        created_a = client.post(
            "/convert/json?background=true",
            headers=headers_a,
            files={"file": ("report.docx", b"tenant a")},
        )
        created_b = client.post(
            "/convert/jobs",
            headers=headers_b,
            files={"file": ("report.docx", b"tenant b")},
        )
        session_a = created_a.json()["session_id"]

        cross_tenant_responses = [
            client.get(f"/convert/sessions/{session_a}", headers=headers_b),
            client.get(f"/convert/jobs/{session_a}", headers=headers_b),
            client.get(f"/convert/sessions/{session_a}/events", headers=headers_b),
            client.get(f"/convert/sessions/{session_a}/result", headers=headers_b),
            client.delete(f"/convert/sessions/{session_a}", headers=headers_b),
        ]

        deadline = time.time() + 3
        job_a = created_a.json()
        while job_a["status"] != "completed" and time.time() < deadline:
            time.sleep(0.01)
            job_a = client.get(f"/convert/sessions/{session_a}", headers=headers_a).json()
        result_a = client.get(job_a["result"]["result_url"], headers=headers_a)
        metrics_a = client.get("/metrics", headers=headers_a).text
        metrics_b = client.get("/metrics", headers=headers_b).text

    assert created_a.status_code == 202
    assert created_b.status_code == 202
    assert created_a.json()["session_id"] != created_b.json()["session_id"]
    assert all(response.status_code == 404 for response in cross_tenant_responses)
    assert all(
        response.json()["error"]["code"] == "job_not_found" for response in cross_tenant_responses
    )
    assert job_a["status"] == "completed"
    assert result_a.text == "# Tenant A"
    assert 'all2md_jobs{status="completed"} 1' in metrics_a
    assert 'all2md_jobs{status="completed"} 1' in metrics_b
    assert 'status="404"' not in metrics_a
    assert 'status="404"' in metrics_b


def test_authentication_rejects_before_upload_processing(tmp_path) -> None:
    app = create_app(
        settings_for(
            tmp_path,
            api_key="a-production-key-with-entropy",
            max_upload_bytes=1,
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/convert",
            files={"file": ("large.pdf", b"a payload larger than the limit")},
        )

    assert response.status_code == 401
    assert not any((tmp_path / "tmp").iterdir())


def test_convert_json_accepts_backend_and_reports_used_engine(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch, content="# Report", backend="anydoc")
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/convert/json?backend=anydoc",
            files={"file": ("report.docx", b"document bytes")},
        )

    assert response.status_code == 200
    assert response.json()["content"] == "# Report"
    assert response.json()["backend"] == "anydoc"
    assert len(response.json()["sha256"]) == 64


def test_plain_text_response_exposes_backend_header(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch, content="# PDF", backend="liteparse")
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/convert?backend=liteparse",
            files={"file": ("report.pdf", b"pdf bytes")},
        )

    assert response.status_code == 200
    assert response.headers["X-All2MD-Backend"] == "liteparse"


def test_upload_limit_is_enforced_while_streaming(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch)
    app = create_app(settings_for(tmp_path, max_upload_bytes=4))

    with TestClient(app) as client:
        response = client.post(
            "/convert/json",
            files={"file": ("large.docx", b"12345")},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert not any((tmp_path / "tmp").iterdir())


def test_chunked_request_body_has_an_asgi_level_limit(tmp_path) -> None:
    app = create_app(settings_for(tmp_path, max_upload_bytes=4))
    boundary = "all2md-boundary"

    def chunks():
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="large.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        yield b"x" * (1024 * 1024 + 8)
        yield f"\r\n--{boundary}--\r\n".encode()

    with TestClient(app) as client:
        response = client.post(
            "/convert",
            content=chunks(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"


def test_invalid_backend_has_stable_error_shape(tmp_path) -> None:
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/convert?backend=unknown",
            files={"file": ("report.pdf", b"pdf bytes")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["request_id"]


def test_durable_job_completes_and_result_is_downloadable(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch, content="# Durable", backend="anydoc")
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/convert/jobs?backend=anydoc",
            files={"file": ("../report.docx", b"document bytes")},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        deadline = time.time() + 3
        job = created.json()
        while job["status"] != "completed" and time.time() < deadline:
            time.sleep(0.02)
            job = client.get(f"/convert/jobs/{job_id}").json()
        result = client.get(f"/convert/jobs/{job_id}/result")

    assert job["status"] == "completed"
    assert job["filename"] == "report.docx"
    assert "input_path" not in job
    assert result.status_code == 200
    assert result.text == "# Durable"
    assert result.headers["X-All2MD-Backend"] == "anydoc"
    assert not list((tmp_path / "jobs" / job_id).glob("input.*"))


def test_job_query_reports_backend_milestones_without_cache(tmp_path, monkeypatch) -> None:
    async def progressing_run(self, **arguments):
        await arguments["progress_callback"](
            RuntimeProgress(
                sequence=1,
                stage="converting",
                percent=42,
                message="Trying anydoc backend (1 of 3)",
                backend=ConversionBackend.ANYDOC,
                attempt=1,
                attempts=3,
            )
        )
        await arguments["heartbeat_callback"]()
        await asyncio.sleep(0.3)
        output_path = arguments["output_path"]
        output_path.write_text("# Progress", encoding="utf-8")
        return RuntimeResult(
            output_path=output_path,
            backend=ConversionBackend.ANYDOC,
            size_bytes=10,
        )

    monkeypatch.setattr("all2md.conversion_runtime.ConversionRuntime.run", progressing_run)
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/convert/jobs",
            files={"file": ("report.docx", b"document bytes")},
        ).json()
        job_id = created["job_id"]
        deadline = time.time() + 3
        response = client.get(f"/convert/jobs/{job_id}")
        while response.json()["stage"] != "converting" and time.time() < deadline:
            time.sleep(0.01)
            response = client.get(f"/convert/jobs/{job_id}")
        running = response.json()

        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Retry-After"] == "1"
        assert int(response.headers["X-Progress-Revision"]) >= 2
        assert running["status"] == "running"
        assert running["progress"] == 42
        assert running["progress_accuracy"] == "milestone"
        assert running["current_backend"] == "anydoc"
        assert (running["backend_attempt"], running["backend_attempts"]) == (1, 3)
        assert running["stage_elapsed_seconds"] >= 0
        assert running["heartbeat_at"] is not None

        while response.json()["status"] != "completed" and time.time() < deadline:
            time.sleep(0.01)
            response = client.get(f"/convert/jobs/{job_id}")

    completed = response.json()
    assert completed["progress"] == 100
    assert completed["progress_accuracy"] == "exact"
    assert completed["stage"] == "completed"
    assert (completed["backend_attempt"], completed["backend_attempts"]) == (1, 3)
    assert "Retry-After" not in response.headers


def test_job_sse_streams_progress_and_terminal_event(tmp_path, monkeypatch) -> None:
    async def progressing_run(self, **arguments):
        await arguments["progress_callback"](
            RuntimeProgress(
                sequence=1,
                stage="converting",
                percent=42,
                message="Trying anydoc backend (1 of 3)",
                backend=ConversionBackend.ANYDOC,
                attempt=1,
                attempts=3,
            )
        )
        await asyncio.sleep(0.25)
        await arguments["heartbeat_callback"]()
        await asyncio.sleep(0.25)
        output_path = arguments["output_path"]
        output_path.write_text("# SSE", encoding="utf-8")
        return RuntimeResult(
            output_path=output_path,
            backend=ConversionBackend.ANYDOC,
            size_bytes=5,
        )

    monkeypatch.setattr("all2md.conversion_runtime.ConversionRuntime.run", progressing_run)
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/convert/jobs",
            files={"file": ("report.docx", b"document bytes")},
        ).json()
        with client.stream("GET", created["events_url"]) as response:
            events = parse_sse_events(response.iter_lines())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["Cache-Control"] == "no-cache, no-store"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert events[0]["event"] in {"snapshot", "progress"}
    assert events[0]["retry"] == 1000
    assert "heartbeat" in {event["event"] for event in events}
    assert events[-1]["event"] == "completed"
    assert events[-1]["data"]["progress"] == 100
    assert events[-1]["data"]["events_url"] == created["events_url"]
    event_ids = [event["id"] for event in events if "id" in event]
    assert event_ids == sorted(set(event_ids))


def test_job_sse_supports_last_event_id_and_validates_revisions(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch)
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/convert/jobs",
            files={"file": ("report.docx", b"document bytes")},
        ).json()
        job_id = created["job_id"]
        deadline = time.time() + 3
        job = created
        while job["status"] != "completed" and time.time() < deadline:
            time.sleep(0.01)
            job = client.get(f"/convert/jobs/{job_id}").json()

        with client.stream(
            "GET",
            created["events_url"],
            headers={"Last-Event-ID": str(job["progress_revision"] - 1)},
        ) as resumed:
            events = parse_sse_events(resumed.iter_lines())
        invalid = client.get(
            created["events_url"],
            headers={"Last-Event-ID": "not-an-integer"},
        )
        future = client.get(
            created["events_url"],
            headers={"Last-Event-ID": str(job["progress_revision"] + 1)},
        )

    assert events[-1]["event"] == "completed"
    assert events[-1]["id"] == job["progress_revision"]
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_last_event_id"
    assert future.status_code == 409
    assert future.json()["error"]["code"] == "future_progress_revision"


def test_metrics_report_persisted_job_counts(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch)
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        client.post(
            "/convert/jobs",
            files={"file": ("report.docx", b"document bytes")},
        )
        deadline = time.time() + 3
        body = ""
        while 'all2md_jobs{status="completed"} 1' not in body and time.time() < deadline:
            time.sleep(0.02)
            body = client.get("/metrics").text

    assert "all2md_http_requests_total" in body
    assert 'all2md_jobs{status="completed"} 1' in body


def test_rate_limit_is_shared_through_durable_store(tmp_path) -> None:
    app = create_app(settings_for(tmp_path, rate_limit_per_minute=1))

    with TestClient(app) as client:
        first = client.get("/backends")
        limited = client.get("/backends")

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(limited.headers["Retry-After"]) >= 1


def test_idempotency_key_replays_existing_job(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch)
    app = create_app(settings_for(tmp_path))
    headers = {"Idempotency-Key": "same-client-operation"}

    with TestClient(app) as client:
        first = client.post(
            "/convert/jobs",
            headers=headers,
            files={"file": ("report.docx", b"first")},
        )
        second = client.post(
            "/convert/jobs",
            headers=headers,
            files={"file": ("report.docx", b"first")},
        )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["idempotency_replayed"] is True


def test_idempotency_key_rejects_a_different_payload(tmp_path, monkeypatch) -> None:
    install_fake_runtime(monkeypatch)
    app = create_app(settings_for(tmp_path))
    headers = {"Idempotency-Key": "must-identify-one-request"}

    with TestClient(app) as client:
        first = client.post(
            "/convert/jobs",
            headers=headers,
            files={"file": ("report.docx", b"first")},
        )
        conflict = client.post(
            "/convert/jobs",
            headers=headers,
            files={"file": ("report.docx", b"different")},
        )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_running_job_can_be_cancelled(tmp_path, monkeypatch) -> None:
    async def cancellable_run(self, **arguments):
        while not await arguments["cancellation_check"]():
            await asyncio.sleep(0.01)
        raise ConversionCancelledError("cancelled")

    monkeypatch.setattr("all2md.conversion_runtime.ConversionRuntime.run", cancellable_run)
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/convert/jobs",
            files={"file": ("report.pdf", b"pdf")},
        ).json()
        job_id = created["job_id"]
        deadline = time.time() + 3
        job = created
        while job["status"] == "queued" and time.time() < deadline:
            time.sleep(0.02)
            job = client.get(f"/convert/jobs/{job_id}").json()
        client.delete(f"/convert/jobs/{job_id}")
        while job["status"] != "cancelled" and time.time() < deadline:
            time.sleep(0.02)
            job = client.get(f"/convert/jobs/{job_id}").json()

    assert job["status"] == "cancelled"
    assert job["stage"] == "cancelled"
    assert job["progress"] < 100
    assert job["progress_accuracy"] == "milestone"
    assert not list((tmp_path / "jobs" / job_id).glob("input.*"))


def test_failed_job_retains_last_progress_milestone(tmp_path, monkeypatch) -> None:
    async def failed_run(self, **arguments):
        await arguments["progress_callback"](
            RuntimeProgress(
                sequence=1,
                stage="converting",
                percent=37,
                message="Trying liteparse backend (1 of 4)",
                backend=ConversionBackend.LITEPARSE,
                attempt=1,
                attempts=4,
            )
        )
        raise ConversionProcessError("failed")

    monkeypatch.setattr("all2md.conversion_runtime.ConversionRuntime.run", failed_run)
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        created = client.post(
            "/convert/jobs",
            files={"file": ("report.pdf", b"pdf")},
        ).json()
        deadline = time.time() + 3
        job = created
        while job["status"] != "failed" and time.time() < deadline:
            time.sleep(0.01)
            job = client.get(f"/convert/jobs/{created['job_id']}").json()

    assert job["status"] == "failed"
    assert job["stage"] == "failed"
    assert job["progress"] == 37
    assert job["progress_accuracy"] == "milestone"
    assert job["current_backend"] == "liteparse"


def test_conversion_errors_do_not_expose_worker_diagnostics(tmp_path, monkeypatch) -> None:
    async def failed_run(self, **arguments):
        raise ConversionProcessError("secret path C:/private/report.pdf")

    monkeypatch.setattr("all2md.conversion_runtime.ConversionRuntime.run", failed_run)
    app = create_app(settings_for(tmp_path))

    with TestClient(app) as client:
        response = client.post(
            "/convert",
            files={"file": ("report.pdf", b"pdf")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "conversion_failed"
    assert "secret" not in response.text
    assert "C:/" not in response.text
