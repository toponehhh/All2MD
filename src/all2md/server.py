from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .conversion_runtime import ConversionRuntime
from .converter import ConversionBackend, get_backend_status
from .errors import (
    All2MDError,
    ConversionBusyError,
    ConversionCancelledError,
    ConversionProcessError,
    ConversionTimeoutError,
    EmptyUploadError,
    IdempotencyConflictError,
    QueueFullError,
    ResultTooLargeError,
    UploadTooLargeError,
)
from .job_manager import JobManager
from .job_store import TERMINAL_STATUSES, JobRecord, JobStore
from .middleware import RequestSizeLimitMiddleware
from .observability import Metrics, configure_logging
from .uploads import safe_suffix, save_upload

logger = logging.getLogger(__name__)


class ServiceHttpError(All2MDError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def _error_payload(code: str, message: str, request_id: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _job_payload(job: JobRecord) -> dict[str, object]:
    payload = job.public_dict()
    payload["session_id"] = job.job_id
    payload["status_url"] = f"/convert/sessions/{job.job_id}"
    payload["events_url"] = f"/convert/sessions/{job.job_id}/events"
    if job.status == "completed":
        payload["result"] = {
            "filename": job.filename,
            "content_type": job.content_type,
            "backend": job.result_backend,
            "result_url": f"/convert/sessions/{job.job_id}/result",
        }
    return payload


def _sse_event(
    event: str,
    payload: dict[str, object],
    *,
    event_id: int | None = None,
    retry_ms: int | None = None,
) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def _progress_signature(job: JobRecord) -> tuple[object, ...]:
    return (
        job.status,
        job.progress,
        job.stage,
        job.progress_accuracy,
        job.current_backend,
        job.backend_attempt,
        job.backend_attempts,
        job.message,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = JobStore(settings.database_path, settings.jobs_dir)
    runtime = ConversionRuntime(settings)
    manager = JobManager(settings, store, runtime)
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_logging(settings.log_json)
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        await manager.start()
        logger.info(
            "All2MD service started with conversion_concurrency=%d max_upload_bytes=%d",
            settings.conversion_concurrency,
            settings.max_upload_bytes,
        )
        try:
            yield
        finally:
            await manager.close()
            logger.info("All2MD service stopped")

    application = FastAPI(
        title="All2MD API",
        description="Production document-to-Markdown conversion service",
        version="0.3.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    application.state.settings = settings
    application.state.store = store
    application.state.runtime = runtime
    application.state.manager = manager
    application.state.metrics = metrics

    application.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=[
                "Content-Type",
                "Idempotency-Key",
                "Last-Event-ID",
                settings.api_key_header,
                "X-Request-ID",
            ],
            expose_headers=[
                "X-All2MD-Backend",
                "X-Progress-Revision",
                "X-Request-ID",
            ],
        )
    if settings.allowed_hosts:
        application.add_middleware(
            TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts)
        )

    # Multipart framing gets a small allowance; save_upload enforces the exact file limit.
    application.add_middleware(
        RequestSizeLimitMiddleware,
        max_body_bytes=settings.max_upload_bytes + 1024 * 1024,
    )

    @application.middleware("http")
    async def production_middleware(request: Request, call_next):
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if supplied_request_id
            and len(supplied_request_id) <= 128
            and supplied_request_id.isprintable()
            else uuid4().hex
        )
        request.state.request_id = request_id
        started = time.perf_counter()
        public_path = (
            request.url.path in {"/health", "/ready", "/openapi.json"}
            or request.url.path.startswith("/docs")
            or request.url.path.startswith("/redoc")
        )
        response = None
        if not public_path:
            candidate = request.headers.get(settings.api_key_header)
            owner_id = settings.authenticate_api_key(candidate)
            if owner_id is None:
                response = JSONResponse(
                    status_code=401,
                    content=_error_payload(
                        "invalid_api_key", "A valid API key is required", request_id
                    ),
                    headers={"WWW-Authenticate": "ApiKey"},
                )
            else:
                request.state.owner_id = owner_id
                allowed, retry_after = await asyncio.to_thread(
                    store.consume_rate_limit,
                    owner_id,
                    settings.rate_limit_per_minute,
                )
                if not allowed:
                    response = JSONResponse(
                        status_code=429,
                        content=_error_payload(
                            "rate_limit_exceeded",
                            "Request rate limit exceeded",
                            request_id,
                        ),
                        headers={"Retry-After": str(retry_after)},
                    )

        content_length = request.headers.get("content-length")
        if response is None and request.method == "POST" and content_length:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                declared_bytes = 0
            multipart_allowance = 2 * 1024 * 1024
            if declared_bytes > settings.max_upload_bytes + multipart_allowance:
                response = JSONResponse(
                    status_code=413,
                    content=_error_payload(
                        "upload_too_large",
                        "Request body exceeds the configured upload limit",
                        request_id,
                    ),
                )

        if response is None:
            response = await call_next(request)
        duration = time.perf_counter() - started
        matched_route = request.scope.get("route")
        route = getattr(matched_route, "path", "__rejected__")
        metrics.observe_request(
            getattr(request.state, "owner_id", None),
            request.method,
            route,
            response.status_code,
            duration,
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers.setdefault("Cache-Control", "no-store")
        logger.info(
            "HTTP request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": route,
                "status_code": response.status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )
        return response

    @application.exception_handler(ServiceHttpError)
    async def service_error_handler(request: Request, exc: ServiceHttpError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                exc.code, exc.message, getattr(request.state, "request_id", uuid4().hex)
            ),
            headers=exc.headers,
        )

    @application.exception_handler(UploadTooLargeError)
    async def body_too_large_handler(request: Request, exc: UploadTooLargeError):
        return JSONResponse(
            status_code=413,
            content=_error_payload(
                "upload_too_large",
                str(exc),
                getattr(request.state, "request_id", uuid4().hex),
            ),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                **_error_payload(
                    "invalid_request",
                    "Request parameters are invalid",
                    getattr(request.state, "request_id", uuid4().hex),
                ),
                "details": exc.errors(),
            },
        )

    @application.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", uuid4().hex)
        logger.exception("Unhandled API error request_id=%s", request_id)
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                "internal_error", "An internal server error occurred", request_id
            ),
        )

    api_key_header = APIKeyHeader(name=settings.api_key_header, auto_error=False)

    async def require_api_key(
        request: Request,
        candidate: str | None = Depends(api_key_header),
    ) -> None:
        owner_id = settings.authenticate_api_key(candidate)
        if owner_id is None:
            raise ServiceHttpError(
                401,
                "invalid_api_key",
                "A valid API key is required",
                {"WWW-Authenticate": "ApiKey"},
            )
        request.state.owner_id = owner_id

    def request_owner_id(request: Request) -> str:
        owner_id = getattr(request.state, "owner_id", None)
        if not isinstance(owner_id, str):
            raise ServiceHttpError(401, "invalid_api_key", "A valid API key is required")
        return owner_id

    protected = [Depends(require_api_key)]

    @application.get("/health")
    async def health_check():
        """Process liveness probe. It deliberately avoids dependencies."""
        return {"status": "ok", "version": application.version}

    @application.get("/ready")
    async def readiness_check():
        """Readiness probe for durable storage and required conversion packages."""
        try:
            await asyncio.to_thread(store.check)
            statuses = get_backend_status()
            required = {"anydoc", "liteparse"}
            unavailable = [
                item["name"]
                for item in statuses
                if item["name"] in required and not item["available"]
            ]
            if unavailable:
                raise RuntimeError(f"Unavailable backends: {', '.join(unavailable)}")
        except Exception:
            logger.exception("Readiness check failed")
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready"}

    @application.get("/metrics", dependencies=protected)
    async def prometheus_metrics(request: Request):
        owner_id = request_owner_id(request)
        counts = await asyncio.to_thread(store.counts, owner_id)
        return PlainTextResponse(
            metrics.render(counts, owner_id),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @application.get("/backends", dependencies=protected)
    async def list_backends():
        return {"default": ConversionBackend.AUTO.value, "backends": get_backend_status()}

    async def persist_job_upload(
        upload: UploadFile,
        backend: ConversionBackend,
        owner_id: str,
        idempotency_key: str | None,
    ) -> tuple[JobRecord, bool]:
        job_id = uuid4().hex
        job_dir = settings.jobs_dir / job_id
        job_dir.mkdir(parents=True, mode=0o700)
        original_name = upload.filename or "document"
        input_path = job_dir / f"input{safe_suffix(original_name)}"
        try:
            saved = await save_upload(upload, input_path, settings.max_upload_bytes)
            record = await asyncio.to_thread(
                store.create,
                job_id=job_id,
                filename=saved.filename,
                content_type=upload.content_type,
                requested_backend=backend.value,
                size_bytes=saved.size_bytes,
                sha256=saved.sha256,
                input_path=saved.path,
                ttl_seconds=settings.job_ttl_seconds,
                queue_capacity=settings.queue_capacity,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
            )
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        replayed = record.job_id != job_id
        if replayed:
            shutil.rmtree(job_dir, ignore_errors=True)
        else:
            manager.notify()
        return record, replayed

    def normalize_idempotency_key(owner_id: str, raw_key: str | None) -> str | None:
        if raw_key is None:
            return None
        if not raw_key or len(raw_key) > 128 or not raw_key.isprintable():
            raise ServiceHttpError(
                400,
                "invalid_idempotency_key",
                "Idempotency-Key must contain 1 to 128 printable characters",
            )
        return hashlib.sha256(f"{owner_id}\0{raw_key}".encode()).hexdigest()

    async def enqueue_conversion(
        request: Request,
        upload: UploadFile,
        backend: ConversionBackend,
        raw_idempotency_key: str | None,
    ) -> dict[str, object]:
        owner_id = request_owner_id(request)
        idempotency_key = normalize_idempotency_key(owner_id, raw_idempotency_key)
        try:
            job, replayed = await persist_job_upload(
                upload,
                backend,
                owner_id,
                idempotency_key,
            )
            payload = _job_payload(job)
            payload["idempotency_replayed"] = replayed
            return payload
        except UploadTooLargeError as exc:
            raise ServiceHttpError(413, "upload_too_large", str(exc)) from exc
        except EmptyUploadError as exc:
            raise ServiceHttpError(400, "empty_upload", str(exc)) from exc
        except QueueFullError as exc:
            raise ServiceHttpError(503, "queue_full", str(exc), {"Retry-After": "5"}) from exc
        except IdempotencyConflictError as exc:
            raise ServiceHttpError(409, "idempotency_conflict", str(exc)) from exc

    @application.post("/convert/jobs", status_code=202, dependencies=protected)
    async def start_conversion_job(
        request: Request,
        file: UploadFile,
        backend: Annotated[ConversionBackend, Query()] = ConversionBackend.AUTO,
        raw_idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        return await enqueue_conversion(request, file, backend, raw_idempotency_key)

    @application.get("/convert/jobs/{session_id}", dependencies=protected, include_in_schema=False)
    @application.get("/convert/sessions/{session_id}", dependencies=protected)
    async def get_conversion_job(session_id: str, request: Request, response: Response):
        job = await asyncio.to_thread(
            store.get_for_owner,
            session_id,
            request_owner_id(request),
        )
        if job is None:
            raise ServiceHttpError(404, "job_not_found", "Conversion job was not found")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Progress-Revision"] = str(job.progress_revision)
        if job.status not in TERMINAL_STATUSES:
            response.headers["Retry-After"] = "1"
        return _job_payload(job)

    @application.get(
        "/convert/jobs/{session_id}/events",
        dependencies=protected,
        include_in_schema=False,
    )
    @application.get("/convert/sessions/{session_id}/events", dependencies=protected)
    async def stream_conversion_job_events(
        session_id: str,
        request: Request,
        after_revision: Annotated[int | None, Query(ge=0)] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        owner_id = request_owner_id(request)
        job = await asyncio.to_thread(store.get_for_owner, session_id, owner_id)
        if job is None:
            raise ServiceHttpError(404, "job_not_found", "Conversion job was not found")

        resume_revision = after_revision
        if last_event_id is not None:
            try:
                header_revision = int(last_event_id.strip())
            except ValueError as exc:
                raise ServiceHttpError(
                    400,
                    "invalid_last_event_id",
                    "Last-Event-ID must be a non-negative progress revision",
                ) from exc
            if header_revision < 0:
                raise ServiceHttpError(
                    400,
                    "invalid_last_event_id",
                    "Last-Event-ID must be a non-negative progress revision",
                )
            resume_revision = max(resume_revision or 0, header_revision)
        if resume_revision is not None and resume_revision > job.progress_revision:
            raise ServiceHttpError(
                409,
                "future_progress_revision",
                "The requested progress revision is newer than the job state",
            )

        async def event_stream() -> AsyncIterator[str]:
            current = job
            last_revision = resume_revision if resume_revision is not None else -1
            last_signature = (
                _progress_signature(current)
                if resume_revision == current.progress_revision
                else None
            )
            first_event = True
            keepalive_deadline = time.monotonic() + 15
            poll_interval = min(max(settings.worker_poll_seconds, 0.1), 1.0)

            while True:
                if await request.is_disconnected():
                    return
                if current.progress_revision > last_revision:
                    signature = _progress_signature(current)
                    if current.status in TERMINAL_STATUSES:
                        event_name = current.status
                    elif resume_revision is None and first_event:
                        event_name = "snapshot"
                    elif last_signature is not None and signature == last_signature:
                        event_name = "heartbeat"
                    else:
                        event_name = "progress"
                    yield _sse_event(
                        event_name,
                        _job_payload(current),
                        event_id=current.progress_revision,
                        retry_ms=1000 if first_event else None,
                    )
                    first_event = False
                    last_revision = current.progress_revision
                    last_signature = signature
                    keepalive_deadline = time.monotonic() + 15
                    if current.status in TERMINAL_STATUSES:
                        return
                elif current.status in TERMINAL_STATUSES:
                    return

                if time.monotonic() >= keepalive_deadline:
                    yield f": keepalive {int(time.time())}\n\n"
                    keepalive_deadline = time.monotonic() + 15

                await asyncio.sleep(poll_interval)
                refreshed = await asyncio.to_thread(
                    store.get_for_owner,
                    session_id,
                    owner_id,
                )
                if refreshed is None:
                    yield _sse_event(
                        "expired",
                        {
                            "session_id": session_id,
                            "message": "Conversion job is no longer available",
                        },
                    )
                    return
                current = refreshed

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get(
        "/convert/jobs/{session_id}/result",
        dependencies=protected,
        include_in_schema=False,
    )
    @application.get("/convert/sessions/{session_id}/result", dependencies=protected)
    async def get_conversion_result(session_id: str, request: Request):
        job = await asyncio.to_thread(
            store.get_for_owner,
            session_id,
            request_owner_id(request),
        )
        if job is None:
            raise ServiceHttpError(404, "job_not_found", "Conversion job was not found")
        if job.status != "completed" or not job.result_path:
            raise ServiceHttpError(409, "result_not_ready", "Conversion result is not ready")
        result_path = Path(job.result_path)
        if not result_path.is_file():
            raise ServiceHttpError(410, "result_missing", "Conversion result has expired")
        content = await asyncio.to_thread(result_path.read_text, encoding="utf-8")
        return PlainTextResponse(
            content,
            headers={"X-All2MD-Backend": job.result_backend or "unknown"},
        )

    @application.delete(
        "/convert/jobs/{session_id}", dependencies=protected, include_in_schema=False
    )
    @application.delete("/convert/sessions/{session_id}", dependencies=protected)
    async def cancel_conversion_job(session_id: str, request: Request):
        job = await asyncio.to_thread(
            store.cancel,
            session_id,
            settings.job_ttl_seconds,
            owner_id=request_owner_id(request),
        )
        if job is None:
            raise ServiceHttpError(404, "job_not_found", "Conversion job was not found")
        if job.status == "cancelled":
            Path(job.input_path).unlink(missing_ok=True)
        manager.notify()
        return _job_payload(job)

    async def run_synchronous_conversion(
        request: Request,
        upload: UploadFile,
        backend: ConversionBackend,
    ) -> tuple[str, ConversionBackend, str, str | None]:
        if not settings.sync_endpoints_enabled:
            raise ServiceHttpError(
                503,
                "sync_conversion_disabled",
                "Synchronous conversion is disabled; use /convert/jobs",
            )
        request_dir = Path(tempfile.mkdtemp(prefix="request-", dir=settings.temp_dir))
        input_path = request_dir / f"input{safe_suffix(upload.filename or 'document')}"
        output_path = request_dir / "result.md"
        metadata_path = request_dir / "result.json"
        try:
            saved = await save_upload(upload, input_path, settings.max_upload_bytes)
            result = await runtime.run(
                input_path=saved.path,
                output_path=output_path,
                metadata_path=metadata_path,
                backend=backend,
                cancellation_check=request.is_disconnected,
                acquire_timeout=settings.sync_queue_timeout_seconds,
            )
            content = await asyncio.to_thread(result.output_path.read_text, encoding="utf-8")
            return content, result.backend, saved.filename, upload.content_type
        except UploadTooLargeError as exc:
            raise ServiceHttpError(413, "upload_too_large", str(exc)) from exc
        except EmptyUploadError as exc:
            raise ServiceHttpError(400, "empty_upload", str(exc)) from exc
        except ConversionBusyError as exc:
            raise ServiceHttpError(503, "conversion_busy", str(exc), {"Retry-After": "2"}) from exc
        except ConversionTimeoutError as exc:
            raise ServiceHttpError(504, "conversion_timeout", str(exc)) from exc
        except ConversionCancelledError as exc:
            raise ServiceHttpError(499, "client_disconnected", str(exc)) from exc
        except ResultTooLargeError as exc:
            raise ServiceHttpError(413, "result_too_large", str(exc)) from exc
        except ConversionProcessError as exc:
            raise ServiceHttpError(
                422,
                "conversion_failed",
                "The document could not be converted by the selected backend",
            ) from exc
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)

    @application.post("/convert", dependencies=protected)
    async def convert_document(
        request: Request,
        file: UploadFile,
        backend: Annotated[ConversionBackend, Query()] = ConversionBackend.AUTO,
        background: Annotated[bool, Query()] = False,
        raw_idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        if background:
            payload = await enqueue_conversion(request, file, backend, raw_idempotency_key)
            return JSONResponse(status_code=202, content=payload)
        content, used_backend, _, _ = await run_synchronous_conversion(request, file, backend)
        return PlainTextResponse(
            content,
            headers={"X-All2MD-Backend": used_backend.value},
        )

    @application.post("/convert/json", dependencies=protected)
    async def convert_document_json(
        request: Request,
        file: UploadFile,
        backend: Annotated[ConversionBackend, Query()] = ConversionBackend.AUTO,
        background: Annotated[bool, Query()] = False,
        raw_idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        if background:
            payload = await enqueue_conversion(request, file, backend, raw_idempotency_key)
            return JSONResponse(status_code=202, content=payload)
        content, used_backend, filename, content_type = await run_synchronous_conversion(
            request, file, backend
        )
        return {
            "filename": filename,
            "content": content,
            "content_type": content_type,
            "backend": used_backend.value,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("all2md.server:app", host="0.0.0.0", port=8000)
