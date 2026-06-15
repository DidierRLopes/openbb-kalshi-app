"""ASGI entry point. The application lives in the kalshi package."""

from __future__ import annotations

from kalshi import create_app

app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=7779)
