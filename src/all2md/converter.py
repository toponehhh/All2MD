from pathlib import Path

from markitdown import MarkItDown


def convert_file(input_path: Path, output_path: Path | None = None) -> Path:
    """Convert a document file to Markdown and return the output path."""
    src = Path(input_path)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Input file not found: {src}")

    destination = Path(output_path) if output_path else src.with_suffix(".md")
    destination.parent.mkdir(parents=True, exist_ok=True)

    converter = MarkItDown()
    result = converter.convert(str(src))
    destination.write_text(result.text_content, encoding="utf-8")
    return destination


def convert_bytes(file_bytes: bytes, filename: str) -> str:
    """Convert file bytes to Markdown content and return as string."""
    import tempfile
    
    converter = MarkItDown()
    
    # Create a temporary file to hold the uploaded file
    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    
    try:
        result = converter.convert(tmp_path)
        return result.text_content
    finally:
        # Clean up temporary file
        Path(tmp_path).unlink()
