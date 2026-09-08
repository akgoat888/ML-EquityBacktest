from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_JOB_PUBLIC = ("status", "progress", "total", "universe", "error", "as_of", "n", "deep", "next_cursor")
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
    from aieq.universe import universe_tickers

    tickers = universe_tickers(universe)
    nxt = 0 if cursor is None else max(0, int(cursor))
    if cursor is None:
        job = _load_board_job()
        same = (
            job.get("status") == "running"
            and job.get("universe") == universe
            and bool(job.get("deep")) == deep
        )
        if same:
            nxt = int(job.get("next_i") or job.get("progress") or 0)
    job = {
        "status": "running",
        "progress": nxt,
        "total": len(tickers),
        "universe": universe,
        "error": None,
        "as_of": None,
        "n": 0,
        "deep": deep,
        "tickers": tickers,
        "next_i": nxt,
        "next_cursor": nxt,
    }
    return _advance_board(job, max_workers)


def _advance_board(job: dict[str, Any], max_workers: int) -> dict[str, Any]:
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
