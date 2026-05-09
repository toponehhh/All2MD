from pathlib import Path

import pytest

from all2md.converter import convert_bytes, convert_file


def test_missing_input_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.docx"
    with pytest.raises(FileNotFoundError):
        convert_file(missing)


def test_convert_file_prefers_docling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "sample.txt"
    src.write_text("hello", encoding="utf-8")

    called = {"docling": 0, "markitdown": 0}

    def fake_docling(path: Path) -> str:
        called["docling"] += 1
        return "docling output"

    def fake_markitdown(path: Path) -> str:
        called["markitdown"] += 1
        return "markitdown output"

    monkeypatch.setattr("all2md.converter._convert_with_docling", fake_docling)
    monkeypatch.setattr("all2md.converter._convert_with_markitdown", fake_markitdown)

    out = convert_file(src)

    assert out.read_text(encoding="utf-8") == "docling output"
    assert called["docling"] == 1
    assert called["markitdown"] == 0


def test_convert_file_falls_back_to_markitdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "sample.txt"
    src.write_text("hello", encoding="utf-8")

    called = {"docling": 0, "markitdown": 0}

    def fake_docling(path: Path) -> str:
        called["docling"] += 1
        raise RuntimeError("Docling failed")

    def fake_markitdown(path: Path) -> str:
        called["markitdown"] += 1
        return "markitdown fallback"

    monkeypatch.setattr("all2md.converter._convert_with_docling", fake_docling)
    monkeypatch.setattr("all2md.converter._convert_with_markitdown", fake_markitdown)

    out = convert_file(src)

    assert out.read_text(encoding="utf-8") == "markitdown fallback"
    assert called["docling"] == 1
    assert called["markitdown"] == 1


def test_convert_bytes_falls_back_to_markitdown(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"docling": 0, "markitdown": 0}

    def fake_docling(path: Path) -> str:
        called["docling"] += 1
        raise RuntimeError("Docling failed")

    def fake_markitdown(path: Path) -> str:
        called["markitdown"] += 1
        return "bytes fallback"

    monkeypatch.setattr("all2md.converter._convert_with_docling", fake_docling)
    monkeypatch.setattr("all2md.converter._convert_with_markitdown", fake_markitdown)

    result = convert_bytes(b"hello", "sample.txt")

    assert result == "bytes fallback"
    assert called["docling"] == 1
    assert called["markitdown"] == 1
