from pathlib import Path

import typer

from .converter import convert_file

app = typer.Typer(help="Convert documents into Markdown.")


@app.command()
def convert(
    input_path: Path = typer.Argument(..., help="Path to input document"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output .md file"),
) -> None:
    """Convert one document to Markdown."""
    out_path = convert_file(input_path, output)
    typer.echo(f"Wrote Markdown: {out_path}")


if __name__ == "__main__":
    app()
