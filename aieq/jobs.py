from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
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


def get_state() -> dict[str, Any]:
    with _lock:
        return dict(STATE)


def get_options_state() -> dict[str, Any]:
    with _lock:
        return dict(OPTIONS_STATE)


def get_volume_state() -> dict[str, Any]:
    with _lock:
        return dict(VOLUME_STATE)


def _launch(target, args, name: str) -> None:
    """Vercel kills daemon threads when the request ends, so run scans in-process there."""
    if os.getenv("VERCEL"):
        target(*args)
        return
    threading.Thread(target=target, args=args, daemon=True, name=name).start()


def start_scan(universe: str = "global", deep: bool = False, max_workers: int = 8) -> dict[str, Any]:
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
    except Exception as exc:
        with _lock:
            STATE.update(status="error", error=str(exc))


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
