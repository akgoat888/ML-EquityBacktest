"""Durable store: Postgres when DATABASE_URL is set, otherwise `.cache` files.

Use a hosted Postgres (Neon / Vercel Postgres / Supabase) so boards and tapes
survive deploys. Local `python app.py` without a URL keeps using `.cache/`.
"""
from __future__ import annotations

import json
import os
import pickle
import threading
import time
from datetime import datetime, timezone
from typing import Any

from aieq.config import CACHE_DIR

_SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_docs (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS desk_cache (
    cache_key TEXT PRIMARY KEY,
    payload BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_lock = threading.Lock()
_pool = None
_failed = False


def database_url() -> str | None:
    raw = (
        os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URL")
        or os.getenv("POSTGRES_PRISMA_URL")
        or ""
    ).strip()
    if not raw:
        return None
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    hosted = any(h in raw for h in ("neon.tech", "supabase", "vercel-storage", "rds.amazonaws.com"))
    if hosted and "sslmode=" not in raw:
        raw += ("&" if "?" in raw else "?") + "sslmode=require"
    return raw


def uses_postgres() -> bool:
    return bool(database_url()) and not _failed


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _connect():
    global _pool, _failed
    if _failed:
        return None
    dsn = database_url()
    if not dsn:
        return None
    with _lock:
        if _pool is not None:
            return _pool
        try:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                conninfo=dsn,
                min_size=1,
                max_size=4,
                kwargs={"autocommit": True},
            )
            with pool.connection() as conn:
                conn.execute(_SCHEMA)
            _pool = pool
            return _pool
        except Exception as exc:
            _failed = True
            print(f"Postgres unavailable ({exc}); using .cache files.")
            return None


def ensure_schema() -> bool:
    return _connect() is not None


def backend_name() -> str:
    if _connect() is not None:
        return "postgres"
    return "files"


def _file(kind: str, key: str, *, blob: bool = False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{kind}_{key}")
    return CACHE_DIR / (f"{safe}.pkl" if blob else f"{safe}.json")


def put_doc(kind: str, key: str, payload: Any) -> None:
    body = _jsonable(payload)
    pool = _connect()
    if pool is not None:
        from psycopg.types.json import Jsonb

        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO desk_docs (kind, key, payload, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (kind, key)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                """,
                (kind, key, Jsonb(body)),
            )
    path = _file(kind, key)
    try:
        path.write_text(json.dumps(body, default=str), encoding="utf-8")
    except Exception:
        if pool is None:
            pass


def get_doc(kind: str, key: str) -> Any | None:
    pool = _connect()
    if pool is not None:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT payload FROM desk_docs WHERE kind = %s AND key = %s",
                (kind, key),
            ).fetchone()
        if row and row[0] is not None:
            return row[0]
    path = _file(kind, key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def doc_age_hours(kind: str, key: str) -> float | None:
    pool = _connect()
    if pool is not None:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - updated_at)) FROM desk_docs WHERE kind = %s AND key = %s",
                (kind, key),
            ).fetchone()
        if row and row[0] is not None:
            return float(row[0]) / 3600.0
    path = _file(kind, key)
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 3600.0


def put_blob(key: str, obj: Any) -> None:
    raw = pickle.dumps(obj, protocol=4)
    pool = _connect()
    if pool is not None:
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO desk_cache (cache_key, payload, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (cache_key)
                DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                """,
                (key, raw),
            )
        return
    path = _file("blob", key, blob=True)
    try:
        path.write_bytes(raw)
    except Exception:
        pass


def get_blob(key: str, ttl_hours: float) -> Any | None:
    pool = _connect()
    if pool is not None:
        with pool.connection() as conn:
            row = conn.execute(
                """
                SELECT payload, EXTRACT(EPOCH FROM (NOW() - updated_at))
                FROM desk_cache WHERE cache_key = %s
                """,
                (key,),
            ).fetchone()
        if not row:
            return None
        age_h = float(row[1] or 0) / 3600.0
        if age_h > ttl_hours:
            return None
        try:
            return pickle.loads(bytes(row[0]))
        except Exception:
            return None
    path = _file("blob", key, blob=True)
    if not path.exists():
        return None
    age_h = (time.time() - path.stat().st_mtime) / 3600.0
    if age_h > ttl_hours:
        return None
    try:
        return pickle.loads(path.read_bytes())
    except Exception:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
