from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_JOB_PUBLIC = ("status", "progress", "total", "universe", "error", "as_of", "n", "deep", "next_cursor", "primed")
_advance_lock = threading.Lock()
_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("LAMBDA_TASK_ROOT"))
_BATCH = 5 if _SERVERLESS else 20

STATE: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "total": 0,
    "universe": None,
    "error": None,
    "as_of": None,
    "n": 0,
    "deep": False,
}
OPTIONS_STATE: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "total": 0,
    "universe": None,
    "error": None,
    "as_of": None,
    "n": 0,
}
VOLUME_STATE: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "total": 0,
    "universe": None,
    "error": None,
    "as_of": None,
    "n": 0,
}


def _public(job: dict[str, Any]) -> dict[str, Any]:
    return {k: job.get(k) for k in _JOB_PUBLIC}


def _persist_board_job(job: dict[str, Any]) -> None:
    with _lock:
        STATE.update(_public(job))
    try:
        from aieq.store import put_doc

        put_doc("job", "board", job)
    except Exception:
        pass


def _load_board_job() -> dict[str, Any]:
    try:
        from aieq.store import get_doc

        payload = get_doc("job", "board")
        if isinstance(payload, dict) and payload.get("status"):
            with _lock:
                STATE.update(_public(payload))
            return dict(payload)
    except Exception:
        pass
    with _lock:
        return dict(STATE)


def get_state() -> dict[str, Any]:
    return _public(_load_board_job())


def get_options_state() -> dict[str, Any]:
    with _lock:
        return dict(OPTIONS_STATE)


def get_volume_state() -> dict[str, Any]:
    with _lock:
        return dict(VOLUME_STATE)


def _launch(target, args, name: str) -> None:
    """Vercel kills daemon threads when the request ends, so run scans in-process there."""
    if _SERVERLESS:
        target(*args)
        return
    threading.Thread(target=target, args=args, daemon=True, name=name).start()


def start_scan(
    universe: str = "global",
    deep: bool = False,
    max_workers: int = 8,
    cursor: int | None = None,
) -> dict[str, Any]:
    """Start or continue a universe scan. On Vercel each call scores a small batch."""
    universe = (universe or "global").strip().lower()
    if _SERVERLESS:
        return _start_or_advance_board(universe, bool(deep), max_workers=3, cursor=cursor)
    with _lock:
        if STATE["status"] == "running":
            return dict(STATE)
        STATE.update(
            status="running",
            progress=0,
            total=0,
            universe=universe,
            error=None,
            deep=bool(deep),
        )
    _launch(_run_board, (universe, deep, max_workers), "universe-scan")
    return get_state()


def start_options_scan(universe: str = "mega", year: int | None = None, month: int | None = None) -> dict[str, Any]:
    from aieq.screens import tape_year_month

    year, month = tape_year_month(year, month)
    with _lock:
        if OPTIONS_STATE["status"] == "running":
            return dict(OPTIONS_STATE)
        OPTIONS_STATE.update(status="running", progress=0, total=0, universe=universe, error=None, year=year, month=month)
    _launch(_run_options, (universe, year, month), "options-scan")
    return get_options_state()


def start_volume_scan(universe: str = "mega", year: int | None = None, month: int | None = None) -> dict[str, Any]:
    from aieq.screens import tape_year_month

    year, month = tape_year_month(year, month)
    with _lock:
        if VOLUME_STATE["status"] == "running":
            return dict(VOLUME_STATE)
        VOLUME_STATE.update(status="running", progress=0, total=0, universe=universe, error=None, year=year, month=month)
    _launch(_run_volume, (universe, year, month), "volume-scan")
    return get_volume_state()


def _start_or_advance_board(
    universe: str,
    deep: bool,
    max_workers: int,
    cursor: int | None = None,
) -> dict[str, Any]:
    """On Vercel, the first call only publishes the universe (from the snapshot).

    Scoring a batch inside that same request is what made changing universe feel
    stuck. The poll scores the next slice.
    """
    from aieq.universe import universe_tickers

    existing = _load_board_job()
    same_running = (
        existing.get("status") == "running"
        and existing.get("universe") == universe
        and bool(existing.get("deep")) == bool(deep)
    )
    requested = 0 if cursor is None else max(0, int(cursor))
    if cursor is None and same_running:
        requested = int(existing.get("next_i") or existing.get("progress") or 0)

    # cursor 0 starts over, unless this is the poll that follows a prime
    # (membership already published, first batch not scored yet).
    resume_primed = (
        same_running
        and existing.get("primed")
        and int(existing.get("next_i") or 0) == 0
        and requested == 0
        and cursor is not None
    )
    if requested == 0 and not resume_primed:
        tickers = universe_tickers(universe)
        job = {
            "status": "running",
            "progress": 0,
            "total": len(tickers),
            "universe": universe,
            "error": None,
            "as_of": None,
            "n": 0,
            "deep": deep,
            "tickers": tickers,
            "next_i": 0,
            "next_cursor": 0,
            "primed": True,
        }
        _persist_board_job(job)
        return _public(job)

    tickers = list(existing.get("tickers") or []) if same_running else []
    if not tickers:
        tickers = universe_tickers(universe)
    job = {
        "status": "running",
        "progress": requested,
        "total": len(tickers),
        "universe": universe,
        "error": None,
        "as_of": existing.get("as_of"),
        "n": int(existing.get("n") or 0) if same_running else 0,
        "deep": deep,
        "tickers": tickers,
        "next_i": requested,
        "next_cursor": requested,
        "primed": True,
    }
    return _advance_board(job, max_workers)


def _advance_board(job: dict[str, Any], max_workers: int) -> dict[str, Any]:
    if not _advance_lock.acquire(blocking=False):
        current = _load_board_job()
        return _public(current if current.get("status") else job)
    try:
        return _advance_board_locked(job, max_workers)
    finally:
        _advance_lock.release()


def _advance_board_locked(job: dict[str, Any], max_workers: int) -> dict[str, Any]:
    from aieq.pipeline import score_board_batch

    tickers = list(job.get("tickers") or [])
    nxt = int(job.get("next_i") or 0)
    total = len(tickers) or int(job.get("total") or 0)
    if nxt >= total:
        job.update(
            status="done",
            progress=total,
            total=total,
            as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )
        _persist_board_job(job)
        return _public(job)
    batch = tickers[nxt : nxt + _BATCH]
    try:
        df = score_board_batch(batch, str(job.get("universe") or "global"), deep=bool(job.get("deep")), max_workers=max_workers)
        nxt = nxt + len(batch)
        job.update(
            next_i=nxt,
            next_cursor=nxt,
            progress=nxt,
            total=total,
            n=int(len(df)),
            status="done" if nxt >= total else "running",
            error=None,
            as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        )
        _persist_board_job(job)
        return _public(job)
    except Exception as exc:
        job.update(status="error", error=str(exc))
        _persist_board_job(job)
        return _public(job)


def _run_board(universe: str, deep: bool, max_workers: int) -> None:
    from aieq.pipeline import scan_universe
    from aieq.universe import universe_tickers

    try:
        tickers = universe_tickers(universe)
        with _lock:
            STATE["total"] = len(tickers)

        def progress(done: int, total: int) -> None:
            with _lock:
                STATE["progress"] = done
                STATE["total"] = total
            try:
                from aieq.store import put_doc

                put_doc("job", "board", dict(STATE) | {"tickers": tickers, "next_i": done, "deep": deep})
            except Exception:
                pass

        df = scan_universe(
            tickers,
            deep=deep,
            max_workers=max_workers,
            universe=universe,
            on_progress=progress,
        )
        with _lock:
            STATE.update(
                status="done",
                n=int(len(df)),
                as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                progress=max(STATE["total"], STATE["progress"]),
            )
        _persist_board_job(dict(STATE))
    except Exception as exc:
        with _lock:
            STATE.update(status="error", error=str(exc))
        _persist_board_job(dict(STATE))


def _run_options(universe: str, year: int | None, month: int | None) -> None:
    from aieq.screens import save_screen, scan_option_flags
    from aieq.universe import universe_tickers

    try:
        tickers = universe_tickers(universe)
        with _lock:
            OPTIONS_STATE["total"] = len(tickers)
        rows = scan_option_flags(tickers, year=year, month=month)
        save_screen("options", universe, rows, extra={"year": year, "month": month})
        with _lock:
            OPTIONS_STATE.update(
                status="done",
                n=len(rows),
                progress=len(tickers),
                as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            )
    except Exception as exc:
        with _lock:
            OPTIONS_STATE.update(status="error", error=str(exc))


def _run_volume(universe: str, year: int | None, month: int | None) -> None:
    from aieq.screens import save_screen, scan_volume_flags
    from aieq.universe import universe_tickers

    try:
        tickers = universe_tickers(universe)
        with _lock:
            VOLUME_STATE["total"] = len(tickers)
        rows = scan_volume_flags(tickers, year=year, month=month)
        save_screen("volume", universe, rows, extra={"year": year, "month": month})
        with _lock:
            VOLUME_STATE.update(
                status="done",
                n=len(rows),
                progress=len(tickers),
                as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            )
    except Exception as exc:
        with _lock:
            VOLUME_STATE.update(status="error", error=str(exc))
