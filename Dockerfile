FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Docling and PDF support
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml uv.lock* ./
COPY src ./src
COPY tests ./tests

# Install dependencies
RUN pip install --no-cache-dir -e .

# Expose port
EXPOSE 8000

# Run the server
CMD ["python", "-m", "uvicorn", "all2md.server:app", "--host", "0.0.0.0", "--port", "8000"]
