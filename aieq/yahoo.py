"""Shared Yahoo access: quiet logs, a request gate, and a clean Ctrl+C exit."""
from __future__ import annotations

import atexit
import logging
import threading
import time
from concurrent.futures import thread as cf_thread
from typing import Callable, TypeVar

import yfinance as yf

T = TypeVar("T")

# Parallel scans otherwise stampede Yahoo's crumb endpoint (HTTP 429).
_YF_GATE = threading.BoundedSemaphore(4)
_configured = False


def configure() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    for name in ("yfinance", "yfinance.screener", "peewee", "urllib3", "yfinance.utils"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    # ThreadPoolExecutor registers an atexit join. Workers blocked on Yahoo
    # make Ctrl+C hang in threading._shutdown. Don't wait them out.
    try:
        atexit.unregister(cf_thread._python_exit)
    except Exception:
        pass


def yf_ticker(symbol: str):
    configure()
    return yf.Ticker(symbol)


def gated(fn: Callable[[], T], retries: int = 3) -> T:
    """Run a Yahoo call with a global concurrency cap and 429 backoff."""
    configure()
    delay = 1.6
    last: Exception | None = None
    with _YF_GATE:
        for attempt in range(retries):
            try:
                return fn()
            except Exception as exc:
                last = exc
                msg = str(exc).lower()
                retryable = "429" in msg or "rate" in msg or "crumb" in msg or "too many" in msg
                if attempt + 1 < retries and retryable:
                    time.sleep(delay)
                    delay *= 1.8
                    continue
                raise
    if last:
        raise last
    raise RuntimeError("Yahoo call failed")
