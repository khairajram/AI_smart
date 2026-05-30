"""
start_api.py
────────────
Standalone launcher for the Store Intelligence FastAPI server.
Run from project root: python start_api.py  (or via venv Python)
"""
import sys
import os

# Ensure project root is in sys.path so 'api', 'config', 'reid' packages resolve
sys.path.insert(0, os.path.dirname(__file__))

from api.server import create_app
import uvicorn
from config.settings import settings

# Create the FastAPI app (no registry — standalone API mode)
app = create_app()

if __name__ == "__main__":
    print(f"Starting Store Intelligence API on http://{settings.API_HOST}:{settings.API_PORT}")
    print(f"  API Docs: http://{settings.API_HOST}:{settings.API_PORT}/docs")
    uvicorn.run(
        app,
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.API_RELOAD,
        log_level=settings.LOG_LEVEL.lower(),
    )
