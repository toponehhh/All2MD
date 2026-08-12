# All2MD

Production-oriented document-to-Markdown conversion through a CLI or FastAPI service.

## Conversion routing

All2MD routes documents to specialized local backends and records which backend produced
each result:

1. PDF: LiteParse -> AnyDoc -> Docling -> MarkItDown
2. Images: LiteParse -> Docling -> MarkItDown
3. Office/OpenDocument/RTF/EPUB/CSV: AnyDoc -> Docling -> MarkItDown
4. Other formats: Docling -> AnyDoc -> MarkItDown
5. Markdown/plain text: direct UTF-8 read

Specify `backend=...` or `--backend ...` to force one backend without fallback.
The core installation enables `markitdown[all]`, but automatic routing invokes MarkItDown
only after the specialized backends for that file type have failed.
The upstream `all` extra currently requires a preview release of
`azure-ai-contentunderstanding`; the lock file contains that single explicit prerelease
exception while the rest of the dependency graph remains on stable releases.

## Reliability and security model

- Uploads stream to disk in 1 MB chunks and have a hard configurable size limit.
- Async jobs and rate limits persist in SQLite WAL instead of process memory.
- Job leases recover interrupted work after a crash or restart.
- Each conversion runs in a separate process that can be terminated on timeout or cancel.
- Queue capacity and conversion concurrency provide backpressure.
- Job creation supports tenant-scoped `Idempotency-Key` values for safe client retries.
- API keys map to durable tenant owners and are checked before request bodies are processed.
- Status, SSE, result, cancel, metrics, and rate-limit state are isolated by tenant.
- Stable error codes and request IDs are returned without leaking internal paths.
- Original uploads are deleted as soon as a job reaches a terminal state; Markdown results
  and job metadata expire automatically.
- `/health`, `/ready`, and Prometheus-compatible `/metrics` endpoints support operations.

The bundled durable queue is intended for a single host with a persistent volume. Multiple
Uvicorn processes on that host can share it, but horizontally scaled containers must share
both the SQLite database and job filesystem. For multi-host high availability, replace the
local queue/storage with an external database, object store, and distributed worker service.

## Installation

```bash
uv sync --extra dev
```

Docling is retained as an optional heavyweight compatibility fallback. Install it only
when its document-understanding models are actually required:

```bash
uv sync --extra dev --extra docling
```

Production/source-upload hosts can use:

```bash
python -m pip install -r requirements.txt
```

Compressed audio conversion also requires an `ffmpeg` executable on `PATH`. The production
Docker image includes it; source-upload hosts must provide it in the base environment.

Dependencies should be installed during build/deployment. `run_server.py` does not install
packages at runtime unless `ALL2MD_BOOTSTRAP_DEPENDENCIES=true` is explicitly set for a host
that has no install phase.

## CLI

```bash
uv run all2md convert path/to/file.pdf
uv run all2md convert report.docx --output report.md --backend anydoc
```

## API

Start locally:

```bash
uv run uvicorn all2md.server:app --host 0.0.0.0 --port 8000
```

### Recommended asynchronous flow

Create an idempotent background session. Either use the dedicated job endpoint:

```bash
curl -X POST "http://localhost:8000/convert/jobs?backend=auto" \
  -H "X-API-Key: $TENANT_API_KEY" \
  -H "Idempotency-Key: import-2026-08-12-001" \
  -F "file=@input.pdf"
```

or add `background=true` to either unified conversion endpoint:

```bash
curl -X POST "http://localhost:8000/convert/json?background=true&backend=auto" \
  -H "X-API-Key: $TENANT_API_KEY" \
  -H "Idempotency-Key: import-2026-08-12-001" \
  -F "file=@input.pdf"
```

Both return HTTP 202 with an opaque `session_id` plus `status_url` and `events_url`.
`session_id` currently equals the backward-compatible `job_id`, but clients should use the
session URLs from the response rather than construct them.

Poll status and download the result:

```bash
curl -H "X-API-Key: $TENANT_API_KEY" \
  http://localhost:8000/convert/sessions/SESSION_ID

curl -H "X-API-Key: $TENANT_API_KEY" \
  http://localhost:8000/convert/sessions/SESSION_ID/result
```

For push-based updates, keep one SSE connection open instead of polling:

```bash
curl -N -H "Accept: text/event-stream" \
  -H "X-API-Key: $TENANT_API_KEY" \
  http://localhost:8000/convert/sessions/SESSION_ID/events
```

`GET /convert/sessions/{session_id}/events` sends `snapshot`, `progress`, `heartbeat`,
`completed`, `failed`, or `cancelled` events. Each state event has an integer event ID equal
to `progress_revision`; the terminal event closes the stream. Reconnect with the standard
`Last-Event-ID` header (or `?after_revision=N`) to receive the newest state after that
revision. Intermediate states may be coalesced, so clients should render the newest event,
not count events as units of work.

Browsers using `X-API-Key` should consume the response with streaming `fetch` or an
EventSource-compatible client that supports custom headers. Native browser `EventSource`
cannot attach the API-key header. The endpoint also emits 15-second comment keepalives and
sets proxy-friendly `Cache-Control` and `X-Accel-Buffering` headers.

The status response keeps the compatible integer `progress` field and also reports:

- `stage`: `queued`, `preparing`, `converting`, `selecting_backend`, `backend_failed`,
  `backend_completed`, `finalizing`, `validating`, or a terminal stage.
- `progress_accuracy`: `milestone` until a conversion succeeds and `exact` only at 100%.
  The bundled backends do not expose stable page-level callbacks, so All2MD does not fabricate
  page percentages; failed and cancelled jobs keep their last milestone.
- `current_backend`, `backend_attempt`, and `backend_attempts`: the active fallback attempt.
- `stage_elapsed_seconds` and `heartbeat_at`: stage duration and worker-liveness evidence.
- `progress_revision`: a monotonically increasing value clients can use to ignore stale polls.

Only successfully completed jobs report `progress: 100`. Failed and cancelled jobs retain the
last reached milestone. Status responses include `Cache-Control: no-store`,
`X-Progress-Revision`, and a one-second `Retry-After` hint while work is active.

Cancel queued or running work:

```bash
curl -X DELETE -H "X-API-Key: $TENANT_API_KEY" \
  http://localhost:8000/convert/sessions/SESSION_ID
```

### Compatibility endpoints

With the default `background=false`, `POST /convert` returns Markdown directly and
`POST /convert/json` returns JSON while holding the HTTP connection for the conversion.
With `background=true`, both return the same HTTP 202 session payload as `/convert/jobs`.
The legacy `/convert/jobs/{job_id}` status, SSE, result, and cancel routes remain compatible.

The plain response includes `X-All2MD-Backend`; JSON and job responses include `backend`.

### Operational endpoints

- `GET /health`: lightweight process liveness; intentionally unauthenticated.
- `GET /ready`: SQLite and required-backend readiness; intentionally unauthenticated.
- `GET /metrics`: Prometheus text format; request and job counters are tenant-scoped.
- `GET /backends`: installed backend status.
- `/docs` and `/redoc`: enabled by default and configurable.

## Configuration

| Environment variable | Default | Purpose |
| --- | ---: | --- |
| `ALL2MD_DATA_DIR` | `./data` | Persistent SQLite, inputs, and results |
| `ALL2MD_API_KEYS_JSON` | unset | Multi-tenant JSON map of stable tenant IDs to a key or key list |
| `ALL2MD_API_KEY` | unset | Legacy single-tenant key; use at least 16 characters |
| `ALL2MD_API_KEY_HEADER` | `X-API-Key` | Authentication header name |
| `ALL2MD_MAX_UPLOAD_MB` | `50` | Maximum uploaded document size |
| `ALL2MD_MAX_RESULT_MB` | `25` | Maximum generated Markdown size |
| `ALL2MD_CONVERSION_TIMEOUT_SECONDS` | `300` | Hard child-process timeout |
| `ALL2MD_CONVERSION_CONCURRENCY` | `2` | Maximum conversions per service process |
| `ALL2MD_QUEUE_CAPACITY` | `100` | Maximum queued/running durable jobs |
| `ALL2MD_JOB_TTL_SECONDS` | `86400` | Result retention after terminal status |
| `ALL2MD_RATE_LIMIT_PER_MINUTE` | `120` | Per-tenant fixed-window limit; `0` disables |
| `ALL2MD_SYNC_ENDPOINTS_ENABLED` | `true` | Enable compatibility synchronous endpoints |
| `ALL2MD_SYNC_QUEUE_TIMEOUT_SECONDS` | `2` | How long sync calls wait for capacity |
| `ALL2MD_CLEANUP_INTERVAL_SECONDS` | `300` | Expired job cleanup interval |
| `ALL2MD_WORKER_MEMORY_MB` | `0` | Unix child address-space limit; `0` uses container limit |
| `ALL2MD_ALLOWED_HOSTS` | unset | Comma-separated trusted Host values |
| `ALL2MD_CORS_ORIGINS` | unset | Comma-separated allowed browser origins |
| `ALL2MD_DOCS_ENABLED` | `true` | Expose OpenAPI, Swagger UI, and ReDoc |
| `ALL2MD_LOG_JSON` | `true` | Structured JSON application logs |

LiteParse-specific settings:

- `ALL2MD_LITEPARSE_OCR=false` disables OCR.
- `ALL2MD_LITEPARSE_OCR_LANGUAGE=chi_sim` selects the Tesseract language.
- `ALL2MD_LITEPARSE_MAX_PAGES=200` limits PDF pages.
- `ALL2MD_LITEPARSE_WORKERS=2` controls OCR concurrency inside LiteParse.
- `ALL2MD_LITEPARSE_OCR_SERVER_URL=http://.../ocr` selects a remote OCR server.
- `ALL2MD_LITEPARSE_TESSDATA_PATH=/path/to/tessdata` selects local language data.

For production, configure at least one authentication variable. With neither variable set,
the service intentionally runs in an anonymous default tenant for local development. A tenant
can have two keys during rotation, for example
`{"customer-a":["current-secret...","next-secret..."]}`. Keep the tenant ID unchanged: it is
the durable ownership boundary, while individual secrets may rotate. Cross-tenant access uses
the same 404 response as a missing session, so session existence is not disclosed.
When migrating an existing single-key database, use the tenant ID `default` so historical jobs
remain owned by the same tenant (or keep the legacy `ALL2MD_API_KEY` until they expire).

## Container deployment

```bash
docker build -t all2md:0.3.0 .
docker run --rm -p 8000:8000 \
  --env-file .env \
  --memory 4g --cpus 2 \
  -v all2md-data:/var/lib/all2md \
  all2md:0.3.0
```

The default image deliberately omits the heavyweight optional Docling/Torch stack. It runs as
UID 10001, uses `tini`, has a readiness healthcheck, and installs locked packages during image
build. ImageMagick is included for LiteParse image conversion. Keep `/var/lib/all2md` on a
persistent volume. Terminate TLS and enforce an
infrastructure-level body limit and DDoS protection at Cloudflare or a reverse proxy.

For Katabump/source upload deployments:

1. Set the install command to `python -m pip install -r requirements.txt`.
2. Set the start command to `python run_server.py`.
3. Attach persistent storage and set `ALL2MD_DATA_DIR` to its mount path.
4. Configure `ALL2MD_API_KEYS_JSON` with strong tenant keys; do not commit it.
5. Keep `WEB_CONCURRENCY=1` and tune `ALL2MD_CONVERSION_CONCURRENCY` for available RAM.

## Development gates

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest -q
uv run pip-audit
```

GitHub Actions runs the same lint, tests, dependency audit, and a production Docker build.
