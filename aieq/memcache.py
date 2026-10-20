"""Process memory for lookups, plus an optional durable copy.

A warm function returns the last result without Yahoo or a model run.
The durable copy survives a cold start when Postgres or the cache dir is writable.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_store: dict[str, tuple[float, Any]] = {}
_MAX = 500


def get_mem(key: str, ttl_sec: float) -> Any | None:
    now = time.time()
    with _lock:
        hit = _store.get(key)
        if not hit:
            return None
        saved, value = hit
        if now - saved > ttl_sec:
            _store.pop(key, None)
            return None
        return value


def put_mem(key: str, value: Any) -> None:
    with _lock:
        if len(_store) >= _MAX:
            oldest = sorted(_store, key=lambda k: _store[k][0])[: _MAX // 4]
            for item in oldest:
                _store.pop(item, None)
        _store[key] = (time.time(), value)


def cached_call(key: str, ttl_sec: float, build: Callable[[], Any], *, durable: bool = True) -> Any:
    hit = get_mem(key, ttl_sec)
    if hit is not None:
        return hit
    if durable:
        stored = _read_durable(key, ttl_sec)
        if stored is not None:
            put_mem(key, stored)
            return stored
    body = build()
    put_mem(key, body)
    if durable:
        _write_durable(key, body)
    return body


def _read_durable(key: str, ttl_sec: float) -> Any | None:
    try:
        from aieq.store import get_doc

        doc = get_doc("lookup", key)
    except Exception:
        return None
    if not isinstance(doc, dict) or "body" not in doc:
        return None
    try:
        age = time.time() - float(doc.get("saved") or 0)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > ttl_sec:
        return None
    return doc["body"]


def _write_durable(key: str, body: Any) -> None:
    try:
        from aieq.store import put_doc

        put_doc("lookup", key, {"saved": time.time(), "body": body})
    except Exception:
        pass
