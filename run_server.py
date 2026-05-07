#!/usr/bin/env python
"""Quick start script for the All2MD web API server."""

import uvicorn
from all2md.server import app

if __name__ == "__main__":
    print("Starting All2MD API server...")
    print("📚 Open http://localhost:8000/docs for interactive API documentation")
    print("🔍 API docs: http://localhost:8000/redoc")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
