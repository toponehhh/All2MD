FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN python -m pip install uv==0.11.28

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Build an immutable, non-editable environment from uv.lock. The optional
# Docling/Torch compatibility stack is intentionally excluded from this image.
RUN uv sync --locked --no-editable --no-dev


FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    ALL2MD_DATA_DIR=/var/lib/all2md \
    ALL2MD_LOG_JSON=true

WORKDIR /app

# tini forwards termination signals and reaps isolated conversion processes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini libgomp1 imagemagick ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin all2md \
    && mkdir -p /var/lib/all2md \
    && chown -R all2md:all2md /var/lib/all2md /app

COPY --from=builder --chown=all2md:all2md /app/.venv /app/.venv

USER 10001:10001
VOLUME ["/var/lib/all2md"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "all2md.server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "100", "--timeout-keep-alive", "5"]
