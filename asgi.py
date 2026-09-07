"""Single ASGI app: FastAPI JSON at /api, Flask UI at /.

Used by Vercel and `uvicorn asgi:app`. Local `python app.py` still runs two ports.
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aieq.config  # noqa: F401  — pin caches to /tmp on Vercel before other imports

from fastapi import FastAPI
from fastapi.middleware.wsgi import WSGIMiddleware

from aieq.api import app as api_app
from web.server import app as flask_app


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from aieq.store import ensure_schema
    from aieq.yahoo import configure

    configure()
    ensure_schema()
    yield


app = FastAPI(title="AI Equities Desk", version="2.0.0", lifespan=lifespan)
app.mount("/api", api_app)
app.mount("/", WSGIMiddleware(flask_app))
