from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache"

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
