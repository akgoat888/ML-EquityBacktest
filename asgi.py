"""Single ASGI app: FastAPI JSON at /api, Flask UI at /.

Used by Vercel and `uvicorn asgi:app`. Local `python app.py` still runs two ports.

The API (pandas, models, Yahoo) loads on the first ``/api`` call. A page hit
does not pay for that import.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aieq.config  # noqa: F401  — pin caches to /tmp on Vercel before other imports

from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware

from web.server import app as flask_app


class _LazyApi:
    """Mount that imports the analysis stack only when ``/api`` is called."""

    def __init__(self) -> None:
        self._app = None

    async def __call__(self, scope, receive, send) -> None:
        if self._app is None:
            from aieq.api import app as api_app

            self._app = api_app
        await self._app(scope, receive, send)


app = FastAPI(title="AI Equities Desk", version="2.0.0")
app.mount("/api", _LazyApi())
app.mount("/", WSGIMiddleware(flask_app))
