#!/usr/bin/env python3
"""AI Equities Desk — CLI.

Examples:
  python cli.py NVDA
  python cli.py AAPL MSFT TSLA
  python cli.py --scan
  python cli.py --scan --deep
"""
from __future__ import annotations

import argparse
import json
import sys
from textwrap import fill

from aieq.config import DEFAULT, Settings
from aieq.pipeline import analyze, scan_universe


def _bar(score: float, width: int = 22) -> str:
    score = max(-1.0, min(1.0, score))
    mid = width // 2
    filled = int(round(abs(score) * mid))
    if score >= 0:
        return "[" + " " * mid + "#" * filled + " " * (mid - filled) + "]"
    return "[" + " " * (mid - filled) + "#" * filled + " " * mid + "]"


def render(res) -> str:
    c = res.consensus
    m = res.model_metrics
    b = res.backtest.metrics
    lines = []
    lines.append("=" * 72)
    lines.append(f"  {res.ticker}  {res.name}")
    if getattr(res, "queried", None) and str(res.queried).upper() != str(res.ticker).upper():
        lines.append(f"  resolved from {res.queried}")
    lines.append(f"  as of {res.as_of}   last {res.price:.2f}   model {res.backend}")
    lines.append("=" * 72)
    lines.append("")
    lines.append(f"  RATING      {c.rating:<9}   score {c.score:+.3f}   conf {c.confidence:.0%}   agree {c.agreement:.0%}")
    lines.append(f"  {c.thesis}")
    lines.append("")
    lines.append(f"  XGBoost P(up {DEFAULT.horizon}d)  {m.get('live_p_up', 0):.1%}    E[return] {m.get('live_expected_ret', 0):+.2%}")
    if m.get("oos_auc") is not None:
        lines.append(
            f"  OOS acc {m.get('oos_accuracy', float('nan')):.1%}   AUC {m.get('oos_auc', float('nan')):.3f}   "
            f"IC {m.get('oos_ic', float('nan')):.3f}   n={int(m.get('n_oos') or 0)}"
        )
    lines.append("")
    lines.append("  AGENT SCOREBOARD")
    lines.append("  " + "-" * 68)
    for v in sorted(c.votes, key=lambda x: abs(x.score), reverse=True):
        lines.append(f"  {v.name:<16} {v.score:+6.3f}  {_bar(v.score)}  conf {v.confidence:.0%}")
        lines.append(f"                   {v.reason}")
    lines.append("")
    if b:
        lines.append("  WALK-FORWARD BACKTEST (OOS signals, next-open fill, costs on)")
        lines.append("  " + "-" * 68)
        lines.append(
            f"  CAGR {b.get('cagr', 0):+.1%}   Sharpe {b.get('sharpe', 0):.2f}   Sortino {b.get('sortino', 0):.2f}   "
            f"MaxDD {b.get('max_drawdown', 0):.1%}"
        )
        lines.append(
            f"  Hit {b.get('hit_rate', 0):.1%}   PF {b.get('profit_factor', 0):.2f}   trades {int(b.get('n_trades') or 0)}   "
            f"vs buy&hold {b.get('excess_vs_bh', 0):+.1%}"
        )
        lines.append("")
    if res.options:
        lines.append("  OPTIONS FLOW (factor in the rating, not a call/put ticket)")
        lines.append("  " + "-" * 68)
        for i, o in enumerate(res.options[:4], 1):
            iv_txt = f"{o.iv:.0%}" if 0.05 <= o.iv <= 3.0 else "n/a"
            lines.append(
                f"  {i}. {o.side} {res.ticker} {o.strike:.2f}  exp {o.expiry}  ({o.dte} DTE)  "
                f"mid {o.mid:.2f}  IV {iv_txt}  vol {o.volume}  OI {o.open_interest}"
            )
            lines.append(f"     {o.thesis}")
        lines.append("")
    if getattr(res, "geo_news", None):
        lines.append("  GEOPOLITICS")
        for n in res.geo_news[:5]:
            title = n.get("title") or ""
            pub = n.get("publisher") or ""
            lines.append("  - " + fill(f"{title} ({pub})", width=68, subsequent_indent="    "))
        lines.append("")
    if res.news:
        lines.append("  COMPANY HEADLINES")
        for n in res.news[:6]:
            title = n.get("title") or ""
            pub = n.get("publisher") or ""
            lines.append("  - " + fill(f"{title} ({pub})", width=68, subsequent_indent="    "))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AI Equities Desk — High Buy / Buy / Hold / Sell.")
    p.add_argument("tickers", nargs="*", help="Tickers to analyze")
    p.add_argument("--scan", action="store_true", help="Rank a universe by rating")
    p.add_argument("--universe", default="global", help="mega | global | nasdaq100 | sp500 | midcap | smallcap")
    p.add_argument("--deep", action="store_true", help="Full walk-forward on every scan name (slower)")
    p.add_argument("--horizon", type=int, default=DEFAULT.horizon)
    p.add_argument("--period", default=DEFAULT.period)
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    args = p.parse_args(argv)

    settings = Settings(period=args.period, horizon=args.horizon)

    if args.scan:
        df = scan_universe(settings=settings, deep=args.deep, universe=args.universe)
        if args.json:
            print(df.to_json(orient="records"))
            return 0
        if df.empty:
            print("Scan returned nothing. Check network / Yahoo availability.")
            return 1
        cols = ["ticker", "rating", "score", "confidence", "p_up", "exp_ret", "oos_sharpe", "price"]
        show = df[[c for c in cols if c in df.columns]].copy()
        print("\nUNIVERSE RANKING — High Buy / Buy / Hold / Sell\n")
        print(show.to_string(index=False, float_format=lambda x: f"{x: .3f}"))
        print("\nDeep-dive a name:  python cli.py TICKER\n")
        return 0

    if not args.tickers:
        p.print_help()
        return 2

    from aieq.pipeline import result_to_jsonable

    failed = 0
    for t in args.tickers:
        try:
            res = analyze(t, settings=settings, deep=True)
        except Exception as exc:
            print(f"{t}: {exc}", file=sys.stderr)
            failed += 1
            continue
        if args.json:
            print(json.dumps(result_to_jsonable(res), default=str, indent=2))
        else:
            print(render(res))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
