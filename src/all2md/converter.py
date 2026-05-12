from pathlib import Path

from markitdown import MarkItDown


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
    converter = MarkItDown()
    result = converter.convert(str(src))
    return result.text_content


def _convert_path(src: Path) -> str:
    """Convert with Docling first, then fallback to MarkItDown on failure."""
    if src.suffix.lower() in {".md", ".markdown", ".txt"}:
        return src.read_text(encoding="utf-8", errors="replace")

    try:
        return _convert_with_docling(src)
    except Exception:
        return _convert_with_markitdown(src)


def convert_file(input_path: Path, output_path: Path | None = None) -> Path:
    """Convert a document file to Markdown and return the output path."""
    src = Path(input_path)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")

    destination = Path(output_path) if output_path else src.with_suffix(".md")
    destination.parent.mkdir(parents=True, exist_ok=True)

    markdown = _convert_path(src)
    destination.write_text(markdown, encoding="utf-8")
    return destination


def convert_bytes(file_bytes: bytes, filename: str) -> str:
    """Convert file bytes to Markdown content and return as string."""
    import tempfile
    
    # Create a temporary file to hold the uploaded file
    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    
    try:
        return _convert_path(Path(tmp_path))
    finally:
        # Clean up temporary file
        Path(tmp_path).unlink()
