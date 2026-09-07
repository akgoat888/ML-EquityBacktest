from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _is_serverless() -> bool:
    return bool(
        os.getenv("VERCEL")
        or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        or os.getenv("LAMBDA_TASK_ROOT")
    )


def _configure_serverless_fs() -> None:
    """Vercel/Lambda: /var/task is read-only. Point caches at /tmp."""
    if not _is_serverless():
        return
    tmp = "/tmp"
    os.environ.setdefault("TMPDIR", tmp)
    os.environ.setdefault("XDG_CACHE_HOME", tmp)
    os.environ.setdefault("XDG_DATA_HOME", tmp)
    os.environ.setdefault("AIEQ_CACHE_DIR", f"{tmp}/aieq-cache")
    home = os.getenv("HOME") or ""
    if not home or home.startswith("/var/task") or not os.access(home, os.W_OK):
        os.environ["HOME"] = tmp


def _pick_cache_dir() -> Path:
    override = (os.getenv("AIEQ_CACHE_DIR") or "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    if _is_serverless():
        candidates.append(Path("/tmp/aieq-cache"))
    else:
        candidates.append(ROOT / ".cache")
        candidates.append(Path("/tmp/aieq-cache"))
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_test"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
            return path
        except OSError:
            continue
    return Path("/tmp/aieq-cache")


_configure_serverless_fs()
CACHE_DIR = _pick_cache_dir()


def ensure_cache_dir() -> Path:
    """Create the cache dir if possible; never raise (Vercel /var/task is read-only)."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return CACHE_DIR
    except OSError:
        fallback = Path("/tmp/aieq-cache")
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return fallback


MEGA_LIQUID = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "JPM", "UNH", "XOM", "LLY", "V", "MA", "HD", "COST", "NFLX", "AMD",
    "CRM", "ORCL", "ADBE", "QCOM", "AMAT", "MU", "PLTR", "INTC", "BA",
    "CAT", "GE", "DIS", "WMT", "KO", "PEP", "JNJ", "ABBV", "MRK", "PFE",
    "GS", "MS", "BAC", "C", "WFC", "SPY", "QQQ", "IWM", "DIA", "SMH",
    "XLE", "XLF", "XLK", "GLD", "TLT", "COIN", "MSTR", "ARM",
    "TSM", "ASML", "NVO", "SAP", "BABA",
]

# De-dupe while preserving order
MEGA_LIQUID = list(dict.fromkeys(MEGA_LIQUID))


@dataclass
class Settings:
    period: str = "5y"
    interval: str = "1d"
    horizon: int = 5
    min_train_bars: int = 504
    test_bars: int = 21
    embargo_bars: int = 5
    cost_bps: float = 10.0
    slippage_bps: float = 2.0
    long_threshold: float = 0.55
    short_threshold: float = 0.45
    exit_threshold: float = 0.48
    cache_ttl_hours: float = 4.0
    options_min_dte: int = 14
    options_max_dte: int = 60
    options_target_dte: int = 30
    random_state: int = 42
    finnhub_key: str = field(default_factory=lambda: os.getenv("FINNHUB_API_KEY", ""))
    alpha_vantage_key: str = field(default_factory=lambda: os.getenv("ALPHA_VANTAGE_API_KEY", ""))
    fred_key: str = field(default_factory=lambda: os.getenv("FRED_API_KEY", ""))

    @property
    def round_trip_cost(self) -> float:
        return (self.cost_bps + self.slippage_bps) / 10_000.0


DEFAULT = Settings()
