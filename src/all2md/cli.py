from pathlib import Path
from typing import Annotated

import typer

from .converter import ConversionBackend, convert_file

app = typer.Typer(help="Convert documents into Markdown.")


@app.callback()
def main() -> None:
    """Convert documents into Markdown."""


@app.command()
def convert(
    input_path: Annotated[Path, typer.Argument(help="Path to input document")],
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Output .md file")] = None,
    backend: Annotated[
        ConversionBackend,
        typer.Option(
            "--backend",
            "-b",
            help="Conversion backend: auto, anydoc, liteparse, docling, markitdown, or direct",
        ),
    ] = ConversionBackend.AUTO,
) -> None:
    """Convert one document to Markdown."""
    out_path = convert_file(input_path, output, backend)
    typer.echo(f"Wrote Markdown: {out_path}")


if __name__ == "__main__":
    app()
