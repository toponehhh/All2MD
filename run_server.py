#!/usr/bin/env python
"""Quick start script for the All2MD web API server."""

from __future__ import annotations

import importlib
import importlib.util
import os
import site
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"

# Make the src-layout package importable even when the hosting platform does
# not install the project itself before executing this file.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _ensure_runtime_dependencies() -> None:
    """Validate dependencies; optionally bootstrap source-upload hosting platforms."""
    required_modules = (
        "anydoc",
        "azure.ai.contentunderstanding",
        "azure.ai.documentintelligence",
        "azure.identity",
        "fastapi",
        "lxml",
        "liteparse",
        "mammoth",
        "markitdown",
        "multipart",
        "olefile",
        "openpyxl",
        "pandas",
        "pdfminer",
        "pdfplumber",
        "pydub",
        "pptx",
        "speech_recognition",
        "uvicorn",
        "xlrd",
        "youtube_transcript_api",
    )

    def module_is_available(module: str) -> bool:
        try:
            return importlib.util.find_spec(module) is not None
        except (AttributeError, ImportError, ModuleNotFoundError, ValueError):
            return False

    missing_modules = [module for module in required_modules if not module_is_available(module)]

    if not missing_modules:
        return

    allow_bootstrap = os.getenv("ALL2MD_BOOTSTRAP_DEPENDENCIES", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not allow_bootstrap:
        raise RuntimeError(
            "Missing runtime dependencies: "
            f"{', '.join(missing_modules)}. Install requirements.txt during deployment, "
            "or set ALL2MD_BOOTSTRAP_DEPENDENCIES=true for source-upload hosts."
        )

    requirements = PROJECT_ROOT / "requirements.txt"
    if not requirements.is_file():
        raise RuntimeError(f"Missing dependency file: {requirements}")

    print(
        "Runtime dependencies are missing "
        f"({', '.join(missing_modules)}); installing requirements.txt..."
    )
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements),
        ],
        cwd=PROJECT_ROOT,
    )

    # Some managed containers default pip to a per-user installation. Add that
    # location explicitly so packages installed above are visible immediately.
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        site.addsitedir(user_site)
    else:
        for path in user_site:
            site.addsitedir(path)
    importlib.invalidate_caches()


_ensure_runtime_dependencies()

import uvicorn  # noqa: E402 - dependency bootstrap must run before importing uvicorn

if __name__ == "__main__":
    print("Starting All2MD API server...")
    print("Open http://localhost:8000/docs for interactive API documentation")
    print("API docs: http://localhost:8000/redoc")
    uvicorn.run(
        "all2md.server:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        workers=int(os.getenv("WEB_CONCURRENCY", "1")),
        log_level=os.getenv("LOG_LEVEL", "info"),
        limit_concurrency=int(os.getenv("UVICORN_LIMIT_CONCURRENCY", "100")),
        timeout_keep_alive=int(os.getenv("UVICORN_TIMEOUT_KEEP_ALIVE", "5")),
    )
