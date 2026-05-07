# All2MD

Convert documents into Markdown via CLI or REST API.

## Supported Input Types

Input support depends on what `markitdown` can parse in your environment. Common examples include:

- `.pdf`
- `.docx`
- `.pptx`
- `.html`
- `.txt`

## Quick Start

1. Install `uv` (if not already installed).
2. Sync project dependencies.
3. Run the CLI or start the web API server.

```bash
uv sync --dev
```

## CLI Usage

Convert one file:

```bash
uv run all2md convert path/to/file.pdf
```

Convert to a specific output path:

```bash
uv run all2md convert path/to/file.docx --output docs/file.md
```

Show help:

```bash
uv run all2md --help
```

## Web API Usage

### Start the Server

```bash
uv run python run_server.py
```

Or using the entry point:

```bash
uv run uvicorn all2md.server:app --reload
```

The API will be available at `http://localhost:8000`

### Interactive API Documentation

- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

### API Endpoints

#### Health Check
```bash
curl http://localhost:8000/health
```

#### Convert Document (Returns Plain Text)
```bash
curl -X POST http://localhost:8000/convert \
  -F "file=@input.docx"
```

#### Convert Document (Returns JSON)
```bash
curl -X POST http://localhost:8000/convert/json \
  -F "file=@input.pdf"
```

**Response Example:**
```json
{
  "filename": "document.pdf",
  "content": "# Document Title\n\nContent here...",
  "content_type": "application/pdf"
}
```

### Python Client Example

```python
import requests

# Upload file and get markdown content
with open("document.docx", "rb") as f:
    response = requests.post(
        "http://localhost:8000/convert/json",
        files={"file": f}
    )
    result = response.json()
    print(result["content"])
```

## Development

Run tests:

```bash
uv run pytest -q
```
