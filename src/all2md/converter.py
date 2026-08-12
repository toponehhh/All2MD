from __future__ import annotations

import importlib.util
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ConversionBackend(str, Enum):
    """Document conversion engines exposed by All2MD."""

    AUTO = "auto"
    ANYDOC = "anydoc"
    LITEPARSE = "liteparse"
    DOCLING = "docling"
    MARKITDOWN = "markitdown"
    DIRECT = "direct"


@dataclass(frozen=True)
class ConversionResult:
    """Markdown output together with the engine that produced it."""

    content: str
    backend: ConversionBackend


@dataclass(frozen=True)
class ConversionProgress:
    """A truthful conversion milestone emitted by the backend router."""

    stage: str
    percent: int
    message: str
    backend: ConversionBackend | None = None
    attempt: int = 0
    attempts: int = 0


ProgressCallback = Callable[[ConversionProgress], None]


class ConversionFailedError(RuntimeError):
    """Raised when every selected conversion backend fails."""

    def __init__(self, attempts: list[tuple[ConversionBackend, Exception]]) -> None:
        self.attempts = tuple(attempts)
        details = "; ".join(f"{backend.value}: {error}" for backend, error in attempts)
        super().__init__(f"Document conversion failed ({details})")


TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
ANYDOC_EXTENSIONS = {
    ".doc",
    ".docx",
    ".docm",
    ".ppt",
    ".pps",
    ".pot",
    ".pptx",
    ".pptm",
    ".ppsx",
    ".ppsm",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xlsb",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
    ".epub",
    ".csv",
}

_BACKEND_MODULES = {
    ConversionBackend.ANYDOC: "anydoc",
    ConversionBackend.LITEPARSE: "liteparse",
    ConversionBackend.DOCLING: "docling",
    ConversionBackend.MARKITDOWN: "markitdown",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _convert_with_anydoc(src: Path) -> str:
    """Convert Office documents and text PDFs with AnyDoc."""
    try:
        import anydoc
    except ImportError as exc:
        raise RuntimeError("AnyDoc is not installed (pip install firecrawl-anydoc)") from exc

    markdown = anydoc.to_markdown(str(src))
    if isinstance(markdown, str) and markdown.strip():
        return markdown
    raise ValueError("AnyDoc conversion returned empty content")


def _convert_with_liteparse(src: Path) -> str:
    """Convert PDFs to Markdown with LiteParse and optional OCR."""
    try:
        from liteparse import LiteParse
    except ImportError as exc:
        raise RuntimeError("LiteParse is not installed (pip install liteparse)") from exc

    options: dict[str, object] = {
        "ocr_enabled": _env_bool("ALL2MD_LITEPARSE_OCR", True),
        "ocr_language": os.getenv("ALL2MD_LITEPARSE_OCR_LANGUAGE", "eng"),
        "max_pages": _env_int("ALL2MD_LITEPARSE_MAX_PAGES", 200),
        "num_workers": _env_int("ALL2MD_LITEPARSE_WORKERS", 2),
        "output_format": "markdown",
        "image_mode": "placeholder",
        "quiet": True,
    }
    if ocr_server_url := os.getenv("ALL2MD_LITEPARSE_OCR_SERVER_URL"):
        options["ocr_server_url"] = ocr_server_url
    if tessdata_path := os.getenv("ALL2MD_LITEPARSE_TESSDATA_PATH"):
        options["tessdata_path"] = tessdata_path

    result = LiteParse(**options).parse(str(src))
    markdown = getattr(result, "text", None)
    if isinstance(markdown, str) and markdown.strip():
        return markdown
    raise ValueError("LiteParse conversion returned empty content")


def _convert_with_docling(src: Path) -> str:
    """Convert a file to Markdown using Docling."""
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise RuntimeError("Docling is not installed") from exc

    converter = DocumentConverter()
    result = converter.convert(str(src))

    document = getattr(result, "document", None)
    if document is not None and hasattr(document, "export_to_markdown"):
        markdown = document.export_to_markdown()
        if isinstance(markdown, str) and markdown.strip():
            return markdown

    text_content = getattr(result, "text_content", None)
    if isinstance(text_content, str) and text_content.strip():
        return text_content

    raise ValueError("Docling conversion returned empty content")


def _convert_with_markitdown(src: Path) -> str:
    """Convert a file to Markdown using MarkItDown."""
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise RuntimeError("MarkItDown is not installed") from exc

    result = MarkItDown().convert(str(src))
    markdown = getattr(result, "text_content", None)
    if isinstance(markdown, str) and markdown.strip():
        return markdown
    raise ValueError("MarkItDown conversion returned empty content")


def _normalize_backend(backend: ConversionBackend | str) -> ConversionBackend:
    if isinstance(backend, ConversionBackend):
        return backend
    try:
        return ConversionBackend(backend.lower())
    except ValueError as exc:
        choices = ", ".join(item.value for item in ConversionBackend)
        raise ValueError(f"Unknown backend '{backend}'. Choose one of: {choices}") from exc


def _backend_candidates(src: Path, backend: ConversionBackend) -> tuple[ConversionBackend, ...]:
    if backend is not ConversionBackend.AUTO:
        return (backend,)

    suffix = src.suffix.lower()
    if suffix in TEXT_EXTENSIONS:
        return (ConversionBackend.DIRECT,)
    if suffix in PDF_EXTENSIONS:
        return (
            ConversionBackend.LITEPARSE,
            ConversionBackend.ANYDOC,
            ConversionBackend.DOCLING,
            ConversionBackend.MARKITDOWN,
        )
    if suffix in IMAGE_EXTENSIONS:
        return (
            ConversionBackend.LITEPARSE,
            ConversionBackend.DOCLING,
            ConversionBackend.MARKITDOWN,
        )
    if suffix in ANYDOC_EXTENSIONS:
        return (
            ConversionBackend.ANYDOC,
            ConversionBackend.DOCLING,
            ConversionBackend.MARKITDOWN,
        )
    return (
        ConversionBackend.DOCLING,
        ConversionBackend.ANYDOC,
        ConversionBackend.MARKITDOWN,
    )


def _run_backend(src: Path, backend: ConversionBackend) -> str:
    if backend is ConversionBackend.DIRECT:
        if src.suffix.lower() not in TEXT_EXTENSIONS:
            raise ValueError("The direct backend only supports Markdown and plain-text files")
        return src.read_text(encoding="utf-8", errors="replace")
    if backend is ConversionBackend.ANYDOC:
        return _convert_with_anydoc(src)
    if backend is ConversionBackend.LITEPARSE:
        return _convert_with_liteparse(src)
    if backend is ConversionBackend.DOCLING:
        return _convert_with_docling(src)
    if backend is ConversionBackend.MARKITDOWN:
        return _convert_with_markitdown(src)
    raise ValueError(f"Backend cannot be executed directly: {backend.value}")


def _convert_path_with_backend(
    src: Path,
    backend: ConversionBackend | str = ConversionBackend.AUTO,
    progress_callback: ProgressCallback | None = None,
) -> ConversionResult:
    requested_backend = _normalize_backend(backend)
    attempts: list[tuple[ConversionBackend, Exception]] = []
    candidates = _backend_candidates(src, requested_backend)
    candidate_count = len(candidates)

    for attempt, candidate in enumerate(candidates, start=1):
        start_percent = 15 + ((attempt - 1) * 70 // candidate_count)
        if progress_callback is not None:
            progress_callback(
                ConversionProgress(
                    stage="converting",
                    percent=start_percent,
                    message=(f"Trying {candidate.value} backend ({attempt} of {candidate_count})"),
                    backend=candidate,
                    attempt=attempt,
                    attempts=candidate_count,
                )
            )
        try:
            content = _run_backend(src, candidate)
            if progress_callback is not None:
                progress_callback(
                    ConversionProgress(
                        stage="backend_completed",
                        percent=85,
                        message=f"{candidate.value} backend produced Markdown",
                        backend=candidate,
                        attempt=attempt,
                        attempts=candidate_count,
                    )
                )
            return ConversionResult(content=content, backend=candidate)
        except Exception as exc:
            attempts.append((candidate, exc))
            if progress_callback is not None:
                has_fallback = attempt < candidate_count
                progress_callback(
                    ConversionProgress(
                        stage="selecting_backend" if has_fallback else "backend_failed",
                        percent=15 + (attempt * 70 // candidate_count),
                        message=(
                            f"{candidate.value} backend failed; selecting fallback"
                            if has_fallback
                            else f"{candidate.value} backend failed; no fallback remains"
                        ),
                        backend=candidate,
                        attempt=attempt,
                        attempts=candidate_count,
                    )
                )

    raise ConversionFailedError(attempts)


def _convert_path(
    src: Path,
    backend: ConversionBackend | str = ConversionBackend.AUTO,
) -> str:
    """Convert a path and return Markdown, preserving the original public helper."""
    return _convert_path_with_backend(src, backend).content


def convert_file(
    input_path: Path,
    output_path: Path | None = None,
    backend: ConversionBackend | str = ConversionBackend.AUTO,
) -> Path:
    """Convert a document file to Markdown and return the output path."""
    src = Path(input_path)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")

    destination = Path(output_path) if output_path else src.with_suffix(".md")
    destination.parent.mkdir(parents=True, exist_ok=True)

    result = _convert_path_with_backend(src, backend)
    destination.write_text(result.content, encoding="utf-8")
    return destination


def convert_file_with_backend(
    input_path: Path,
    output_path: Path,
    backend: ConversionBackend | str = ConversionBackend.AUTO,
    *,
    progress_callback: ProgressCallback | None = None,
) -> ConversionResult:
    """Convert a file and return both its Markdown and the selected backend."""
    src = Path(input_path)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")
    result = _convert_path_with_backend(src, backend, progress_callback)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if progress_callback is not None:
        progress_callback(
            ConversionProgress(
                stage="finalizing",
                percent=90,
                message="Writing converted Markdown",
                backend=result.backend,
            )
        )
    destination.write_text(result.content, encoding="utf-8")
    return result


def convert_bytes_with_backend(
    file_bytes: bytes,
    filename: str,
    backend: ConversionBackend | str = ConversionBackend.AUTO,
) -> ConversionResult:
    """Convert uploaded bytes and report which backend produced the Markdown."""
    suffix = Path(filename).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = Path(tmp.name)

    try:
        return _convert_path_with_backend(tmp_path, backend)
    finally:
        tmp_path.unlink(missing_ok=True)


def convert_bytes(
    file_bytes: bytes,
    filename: str,
    backend: ConversionBackend | str = ConversionBackend.AUTO,
) -> str:
    """Convert file bytes to Markdown, preserving the existing string API."""
    return convert_bytes_with_backend(file_bytes, filename, backend).content


def get_backend_status() -> list[dict[str, object]]:
    """Return lightweight backend availability information for API clients."""
    descriptions = {
        ConversionBackend.ANYDOC: "Office, OpenDocument, RTF, EPUB, CSV, and text PDF",
        ConversionBackend.LITEPARSE: "PDF parsing with layout reconstruction and optional OCR",
        ConversionBackend.DOCLING: "Document understanding fallback",
        ConversionBackend.MARKITDOWN: "General-purpose Markdown fallback",
        ConversionBackend.DIRECT: "UTF-8 Markdown and plain text",
    }
    statuses: list[dict[str, object]] = []
    for backend, description in descriptions.items():
        module = _BACKEND_MODULES.get(backend)
        statuses.append(
            {
                "name": backend.value,
                "available": module is None or importlib.util.find_spec(module) is not None,
                "description": description,
            }
        )
    return statuses
