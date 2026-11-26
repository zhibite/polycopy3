"""Run the dashboard: python -m polycopy.web"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "polycopy.web.app:app",
        host=os.environ.get("WEB_HOST", "0.0.0.0"),
        port=int(os.environ.get("WEB_PORT", "8041")),
        log_level=os.environ.get("WEB_LOG_LEVEL", "info"),
    )
