"""Rules for the TA tab. These build price paths in memory — no Yahoo calls."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aieq.ta import analyze_frame, decide_signal, fib_price, judge_franchise, weekly_context


def frame_from_close(close, volume=None) -> pd.DataFrame:
    c = np.asarray(close, dtype=float)
    high = c * 1.003
    low = c * 0.997
    opened = np.roll(c, 1)
    opened[0] = c[0]
    if volume is None:
        volume = np.full(len(c), 1_000_000.0)
    index = pd.bdate_range("2018-01-01", periods=len(c))
    return pd.DataFrame(
        {"open": opened, "high": high, "low": low, "close": c, "volume": np.asarray(volume, dtype=float)},
        index=index,
    )


def _ramp(start, end, n):
    return np.linspace(start, end, n)


class FibMathTests(unittest.TestCase):
    def test_uptrend_levels(self):
        self.assertAlmostEqual(fib_price(10, 15, 0.382, bull=True), 15 - 0.382 * 5, places=6)
        self.assertAlmostEqual(fib_price(10, 15, 0.618, bull=True), 15 - 0.618 * 5, places=6)
        self.assertAlmostEqual(fib_price(10, 15, 1.272, bull=True), 10 + 1.272 * 5, places=6)

    def test_downtrend_levels(self):
        self.assertAlmostEqual(fib_price(15, 10, 0.382, bull=False), 10 + 0.382 * 5, places=6)
        self.assertAlmostEqual(fib_price(15, 10, 0.618, bull=False), 10 + 0.618 * 5, places=6)
        self.assertAlmostEqual(fib_price(15, 10, 1.272, bull=False), 10 - 0.272 * 5, places=6)


class DecideTests(unittest.TestCase):
    def test_no_buy_under_the_200(self):
        out = decide_signal(
            close=90,
            atr=2,
            ma50=110,
            ma200=100,
            ratio=0.55,
            volume_state="Dry pullback",
            bars=260,
            span=40,
        )
        self.assertNotEqual(out["action"], "BUY")
        self.assertEqual(out["trend"], "MIXED")

    def test_uptrend_golden_zone_is_a_buy(self):
        out = decide_signal(
            close=140,
            atr=2,
            ma50=150,
            ma200=100,
            ratio=0.55,
            volume_state="Dry pullback",
            bars=260,
            span=80,
        )
        self.assertEqual(out["action"], "BUY")
        self.assertEqual(out["rating"], "HIGH BUY")
        self.assertEqual(out["trend"], "UP")

    def test_distribution_blocks_the_buy(self):
        out = decide_signal(
            close=140,
            atr=2,
            ma50=150,
            ma200=100,
            ratio=0.55,
            volume_state="Distribution",
            bars=260,
            span=80,
        )
        self.assertEqual(out["action"], "HOLD")

    def test_extended_uptrend_is_not_a_buy(self):
        out = decide_signal(
            close=180,
            atr=2,
            ma50=150,
            ma200=100,
            ratio=0.05,
            volume_state="Neutral",
            bars=260,
            span=80,
        )
        self.assertEqual(out["action"], "HOLD")
        self.assertIn("38.2%", out["why"])

    def test_downtrend_rally_is_a_sell(self):
        out = decide_signal(
            close=120,
            atr=2,
            ma50=130,
            ma200=150,
            ratio=0.55,
            volume_state="Dry pullback",
            bars=260,
            span=50,
        )
        self.assertEqual(out["action"], "SELL")
        self.assertEqual(out["rating"], "HIGH SELL")

    def test_short_history_holds(self):
        out = decide_signal(
            close=10,
            atr=0.2,
            ma50=None,
            ma200=None,
            ratio=None,
            volume_state="Neutral",
            bars=20,
            span=None,
        )
        self.assertEqual(out["action"], "HOLD")


MSFT = {"market_cap": 3_000_000_000_000, "roe": 0.35, "profit_margin": 0.36}


def compounder_dip(volume=None):
    """Years above a rising 200-week line, then a drop through it. The Microsoft shape."""
    rise = _ramp(40, 220, 1700)
    crash = _ramp(220, 130, 40)
    close = np.concatenate([rise, crash])
    if volume is None:
        volume = np.concatenate([
            np.full(len(rise), 1_000_000.0),
            np.linspace(900_000, 500_000, len(crash)),
        ])
    return frame_from_close(close, volume)


class WeeklyBuyTests(unittest.TestCase):
    def test_microsoft_shape_is_a_strong_buy(self):
        frame = compounder_dip()
        ctx = weekly_context(frame["close"])
        self.assertTrue(ctx["structural"], ctx)
        self.assertLess(ctx["dist_wma_200"], 0)
        out = analyze_frame(frame, ticker="MSFT", fundamentals=MSFT)
        self.assertTrue(out["weekly_buy"])
        self.assertEqual(out["action"], "BUY")
        self.assertEqual(out["rating"], "HIGH BUY")
        self.assertIn("200-week", out["why"])
        self.assertLess(out["close"], out["wma_200"])

    def test_distribution_blocks_the_weekly_buy(self):
        rise = _ramp(40, 220, 1700)
        crash = _ramp(220, 130, 40)
        close = np.concatenate([rise, crash])
        volume = np.concatenate([
            np.full(len(rise), 1_000_000.0),
            np.full(len(crash), 4_000_000.0),
        ])
        out = analyze_frame(frame_from_close(close, volume), ticker="MSFT", fundamentals=MSFT)
        self.assertFalse(out["weekly_buy"])
        self.assertNotEqual(out["rating"], "HIGH BUY")

    def test_fund_is_not_a_company(self):
        self.assertFalse(judge_franchise("SPY", MSFT))
        out = analyze_frame(compounder_dip(), ticker="SPY", fundamentals=MSFT)
        self.assertFalse(out["weekly_buy"])

    def test_unprofitable_small_cap_is_not_a_strong_buy(self):
        weak = {"market_cap": 1_200_000_000, "roe": 0.02, "profit_margin": 0.01}
        self.assertFalse(judge_franchise("TINY", weak))
        out = analyze_frame(compounder_dip(), ticker="TINY", fundamentals=weak)
        self.assertFalse(out["weekly_buy"])

    def test_falling_200_week_is_not_a_strong_buy(self):
        up = _ramp(40, 100, 400)
        down = _ramp(100, 35, 1500)
        frame = frame_from_close(np.concatenate([up, down]))
        ctx = weekly_context(frame["close"])
        self.assertFalse(ctx["structural"], ctx)
        out = analyze_frame(frame, ticker="MSFT", fundamentals=MSFT)
        self.assertFalse(out["weekly_buy"])


class FrameTests(unittest.TestCase):
    def test_pullback_in_an_uptrend_buys(self):
        # Rise, dip to mark a swing low, rally, then a dry pullback into ~55% retrace.
        rise = _ramp(50, 100, 190)
        dip = _ramp(100, 82, 10)
        rally = _ramp(82, 170, 28)
        span = 170 - 82
        target = 170 - 0.55 * span
        fade = _ramp(170, target, 12)
        close = np.concatenate([rise, dip, rally, fade])
        volume = np.concatenate(
            [
                np.full(len(rise), 1_000_000.0),
                np.full(len(dip), 1_600_000.0),
                np.linspace(1_200_000, 2_400_000, len(rally)),
                np.linspace(900_000, 500_000, len(fade)),
            ]
        )
        out = analyze_frame(frame_from_close(close, volume), with_candles=True)
        self.assertEqual(out["trend"], "UP")
        self.assertEqual(out["action"], "BUY")
        self.assertGreater(out["close"], out["sma_200"])
        self.assertGreater(out["sma_50"], out["sma_200"])
        self.assertTrue(any(level["action"] == "BUY" for level in out["levels"]))
        self.assertTrue(any(level["action"] == "SELL" for level in out["levels"]))
        self.assertTrue(out["candles"])
        active = [level for level in out["levels"] if level["active"]]
        self.assertEqual(len(active), 1)

    def test_rally_in_a_downtrend_sells(self):
        slide = _ramp(220, 90, 210)
        bounce = _ramp(90, 130, 12)
        drop = _ramp(130, 70, 24)
        span = 130 - 70
        target = 70 + 0.55 * span
        rally = _ramp(70, target, 12)
        close = np.concatenate([slide, bounce, drop, rally])
        volume = np.concatenate(
            [
                np.full(len(slide), 1_200_000.0),
                np.full(len(bounce), 900_000.0),
                np.linspace(1_400_000, 2_200_000, len(drop)),
                np.linspace(800_000, 450_000, len(rally)),
            ]
        )
        out = analyze_frame(frame_from_close(close, volume))
        self.assertEqual(out["trend"], "DOWN")
        self.assertEqual(out["action"], "SELL")
        self.assertLess(out["close"], out["sma_200"])
        self.assertLess(out["sma_50"], out["sma_200"])
        sells = [level for level in out["levels"] if level["label"] in {"38.2%", "50%", "61.8%"}]
        self.assertTrue(sells)
        self.assertTrue(all(level["action"] == "SELL" for level in sells))

    def test_empty_frame(self):
        out = analyze_frame(pd.DataFrame())
        self.assertEqual(out["action"], "HOLD")
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
