from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TENANT_ID = "default"
_TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def owner_id_for_tenant(tenant_id: str) -> str:
    """Return a stable pseudonymous database identifier for a tenant."""
    return hashlib.sha256(f"all2md-tenant\0{tenant_id}".encode()).hexdigest()


DEFAULT_OWNER_ID = owner_id_for_tenant(DEFAULT_TENANT_ID)


def _validate_api_key(name: str, value: str) -> str:
    if len(value) < 16:
        raise RuntimeError(f"{name} must contain at least 16 characters")
    if not value.isprintable():
        raise RuntimeError(f"{name} must contain only printable characters")
    return value


def _api_keys_env() -> tuple[tuple[str, str], ...]:
    raw_value = os.getenv("ALL2MD_API_KEYS_JSON")
    if not raw_value:
        return ()
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ALL2MD_API_KEYS_JSON must be valid JSON") from exc
    if not isinstance(value, dict) or not value:
        raise RuntimeError("ALL2MD_API_KEYS_JSON must be a non-empty JSON object")

    credentials: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for tenant_id, configured_keys in value.items():
        if not isinstance(tenant_id, str) or not _TENANT_ID_PATTERN.fullmatch(tenant_id):
            raise RuntimeError(
                "API key tenant IDs must use 1-64 letters, digits, dots, underscores, or hyphens"
            )
        keys = configured_keys if isinstance(configured_keys, list) else [configured_keys]
        if not keys or any(not isinstance(key, str) for key in keys):
            raise RuntimeError("Each API key tenant must map to a string or non-empty string list")
        for key in keys:
            validated = _validate_api_key(f"API key for tenant {tenant_id}", key)
            if validated in seen_keys:
                raise RuntimeError("The same API key cannot be assigned more than once")
            seen_keys.add(validated)
            credentials.append((tenant_id, validated))
    return tuple(credentials)


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    value = os.getenv(name)
    try:
        parsed = default if value is None else int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return parsed


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    value = os.getenv(name)
    try:
        parsed = default if value is None else float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return parsed


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


@dataclass(frozen=True)
class Settings:
    """Validated runtime settings for the API and conversion workers."""

    data_dir: Path
    max_upload_bytes: int
    max_result_bytes: int
    conversion_timeout_seconds: float
    conversion_concurrency: int
    sync_queue_timeout_seconds: float
    queue_capacity: int
    job_ttl_seconds: int
    cleanup_interval_seconds: int
    worker_poll_seconds: float
    api_key: str | None
    api_keys: tuple[tuple[str, str], ...]
    api_key_header: str
    docs_enabled: bool
    sync_endpoints_enabled: bool
    cors_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    log_json: bool
    worker_memory_mb: int
    rate_limit_per_minute: int

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("ALL2MD_DATA_DIR", "data")).expanduser().resolve()
        api_key = os.getenv("ALL2MD_API_KEY") or None
        if api_key is not None:
            _validate_api_key("ALL2MD_API_KEY", api_key)
        api_keys = _api_keys_env()
        all_configured_keys = [key for _, key in api_keys]
        if api_key is not None and api_key in all_configured_keys:
            raise RuntimeError("ALL2MD_API_KEY must not duplicate ALL2MD_API_KEYS_JSON")

        return cls(
            data_dir=data_dir,
            max_upload_bytes=_int_env("ALL2MD_MAX_UPLOAD_MB", 50) * 1024 * 1024,
            max_result_bytes=_int_env("ALL2MD_MAX_RESULT_MB", 25) * 1024 * 1024,
            conversion_timeout_seconds=_float_env(
                "ALL2MD_CONVERSION_TIMEOUT_SECONDS", 300.0, minimum=1.0
            ),
            conversion_concurrency=_int_env("ALL2MD_CONVERSION_CONCURRENCY", 2),
            sync_queue_timeout_seconds=_float_env(
                "ALL2MD_SYNC_QUEUE_TIMEOUT_SECONDS", 2.0, minimum=0.1
            ),
            queue_capacity=_int_env("ALL2MD_QUEUE_CAPACITY", 100),
            job_ttl_seconds=_int_env("ALL2MD_JOB_TTL_SECONDS", 86_400),
            cleanup_interval_seconds=_int_env("ALL2MD_CLEANUP_INTERVAL_SECONDS", 300),
            worker_poll_seconds=_float_env("ALL2MD_WORKER_POLL_SECONDS", 0.5, minimum=0.05),
            api_key=api_key,
            api_keys=api_keys,
            api_key_header=os.getenv("ALL2MD_API_KEY_HEADER", "X-API-Key"),
            docs_enabled=_bool_env("ALL2MD_DOCS_ENABLED", True),
            sync_endpoints_enabled=_bool_env("ALL2MD_SYNC_ENDPOINTS_ENABLED", True),
            cors_origins=_csv_env("ALL2MD_CORS_ORIGINS"),
            allowed_hosts=_csv_env("ALL2MD_ALLOWED_HOSTS"),
            log_json=_bool_env("ALL2MD_LOG_JSON", True),
            worker_memory_mb=_int_env("ALL2MD_WORKER_MEMORY_MB", 0, minimum=0),
            rate_limit_per_minute=_int_env("ALL2MD_RATE_LIMIT_PER_MINUTE", 120, minimum=0),
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def api_credentials(self) -> tuple[tuple[str, str], ...]:
        credentials = list(self.api_keys)
        if self.api_key is not None:
            credentials.append((DEFAULT_TENANT_ID, self.api_key))
        return tuple(credentials)

    @property
    def authentication_required(self) -> bool:
        return bool(self.api_credentials)

    def authenticate_api_key(self, candidate: str | None) -> str | None:
        """Return a stable owner ID, never the configured tenant label or secret."""
        credentials = self.api_credentials
        if not credentials:
            return DEFAULT_OWNER_ID
        if candidate is None:
            return None
        matched_tenant: str | None = None
        for tenant_id, api_key in credentials:
            if hmac.compare_digest(candidate, api_key):
                matched_tenant = tenant_id
        return owner_id_for_tenant(matched_tenant) if matched_tenant is not None else None
