from pathlib import Path

import pytest

from all2md.converter import (
    ConversionBackend,
    ConversionFailedError,
    ConversionProgress,
    convert_bytes,
    convert_bytes_with_backend,
    convert_file,
    convert_file_with_backend,
)


def test_missing_input_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.docx"
    with pytest.raises(FileNotFoundError):
        convert_file(missing)


def test_text_files_are_read_directly(tmp_path: Path) -> None:
    src = tmp_path / "sample.txt"
    src.write_text("hello", encoding="utf-8")

    out = convert_file(src)

    assert out.read_text(encoding="utf-8") == "hello"


def test_office_files_prefer_anydoc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "sample.docx"
    src.write_bytes(b"placeholder")
    called: list[str] = []

    def fake_anydoc(path: Path) -> str:
        called.append("anydoc")
        return "anydoc output"

    def unexpected_docling(path: Path) -> str:
        called.append("docling")
        return "docling output"

    monkeypatch.setattr("all2md.converter._convert_with_anydoc", fake_anydoc)
    monkeypatch.setattr("all2md.converter._convert_with_docling", unexpected_docling)

    out = convert_file(src)

    assert out.read_text(encoding="utf-8") == "anydoc output"
    assert called == ["anydoc"]


def test_pdf_prefers_liteparse_and_reports_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def fake_liteparse(path: Path) -> str:
        called.append("liteparse")
        return "# Parsed PDF"

    def unexpected_anydoc(path: Path) -> str:
        called.append("anydoc")
        return "anydoc output"

    monkeypatch.setattr("all2md.converter._convert_with_liteparse", fake_liteparse)
    monkeypatch.setattr("all2md.converter._convert_with_anydoc", unexpected_anydoc)

    result = convert_bytes_with_backend(b"%PDF-placeholder", "document.pdf")

    assert result.content == "# Parsed PDF"
    assert result.backend is ConversionBackend.LITEPARSE
    assert called == ["liteparse"]


def test_pdf_falls_back_from_liteparse_to_anydoc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def failed_liteparse(path: Path) -> str:
        called.append("liteparse")
        raise RuntimeError("LiteParse failed")

    def fake_anydoc(path: Path) -> str:
        called.append("anydoc")
        return "anydoc fallback"

    monkeypatch.setattr("all2md.converter._convert_with_liteparse", failed_liteparse)
    monkeypatch.setattr("all2md.converter._convert_with_anydoc", fake_anydoc)

    result = convert_bytes_with_backend(b"%PDF-placeholder", "document.pdf")

    assert result.content == "anydoc fallback"
    assert result.backend is ConversionBackend.ANYDOC
    assert called == ["liteparse", "anydoc"]


def test_progress_events_are_monotonic_and_report_backend_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "document.pdf"
    source.write_bytes(b"%PDF-placeholder")
    events: list[ConversionProgress] = []

    def failed_liteparse(path: Path) -> str:
        raise RuntimeError("LiteParse failed")

    monkeypatch.setattr("all2md.converter._convert_with_liteparse", failed_liteparse)
    monkeypatch.setattr(
        "all2md.converter._convert_with_anydoc",
        lambda path: "AnyDoc fallback",
    )

    result = convert_file_with_backend(
        source,
        tmp_path / "result.md",
        progress_callback=events.append,
    )

    assert result.backend is ConversionBackend.ANYDOC
    assert [event.percent for event in events] == sorted(event.percent for event in events)
    assert [event.stage for event in events] == [
        "converting",
        "selecting_backend",
        "converting",
        "backend_completed",
        "finalizing",
    ]
    assert events[0].backend is ConversionBackend.LITEPARSE
    assert (events[0].attempt, events[0].attempts) == (1, 4)
    assert events[2].backend is ConversionBackend.ANYDOC
    assert (events[2].attempt, events[2].attempts) == (2, 4)
    assert events[-1].percent == 90


def test_image_files_prefer_liteparse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("all2md.converter._convert_with_liteparse", lambda path: "image OCR")

    result = convert_bytes_with_backend(b"image bytes", "scan.png")

    assert result.content == "image OCR"
    assert result.backend is ConversionBackend.LITEPARSE


@pytest.mark.parametrize(
    ("filename", "expected_backends"),
    [
        (
            "document.pdf",
            [
                ConversionBackend.LITEPARSE,
                ConversionBackend.ANYDOC,
                ConversionBackend.DOCLING,
                ConversionBackend.MARKITDOWN,
            ],
        ),
        (
            "scan.png",
            [
                ConversionBackend.LITEPARSE,
                ConversionBackend.DOCLING,
                ConversionBackend.MARKITDOWN,
            ],
        ),
        (
            "report.docx",
            [
                ConversionBackend.ANYDOC,
                ConversionBackend.DOCLING,
                ConversionBackend.MARKITDOWN,
            ],
        ),
        (
            "archive.unknown",
            [
                ConversionBackend.DOCLING,
                ConversionBackend.ANYDOC,
                ConversionBackend.MARKITDOWN,
            ],
        ),
    ],
)
def test_markitdown_is_the_final_automatic_fallback(
    filename: str,
    expected_backends: list[ConversionBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[ConversionBackend] = []

    def fake_backend(path: Path, backend: ConversionBackend) -> str:
        called.append(backend)
        if backend is ConversionBackend.MARKITDOWN:
            return "MarkItDown fallback"
        raise RuntimeError(f"{backend.value} failed")

    monkeypatch.setattr("all2md.converter._run_backend", fake_backend)

    result = convert_bytes_with_backend(b"placeholder", filename)

    assert result.content == "MarkItDown fallback"
    assert result.backend is ConversionBackend.MARKITDOWN
    assert called == expected_backends


def test_forced_backend_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def failed_anydoc(path: Path) -> str:
        raise RuntimeError("AnyDoc failed")

    def unexpected_docling(path: Path) -> str:
        pytest.fail("A forced backend must not fall back")

    monkeypatch.setattr("all2md.converter._convert_with_anydoc", failed_anydoc)
    monkeypatch.setattr("all2md.converter._convert_with_docling", unexpected_docling)

    with pytest.raises(ConversionFailedError, match="anydoc: AnyDoc failed"):
        convert_bytes(b"placeholder", "document.docx", backend="anydoc")


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown backend"):
        convert_bytes(b"hello", "sample.txt", backend="unknown")
