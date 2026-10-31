import unittest

from aieq.upcoming import judge_upcoming

WMT = {
    "ticker": "WMT",
    "market_cap": 500e9,
    "profit_margin": 0.025,
    "roe": 0.18,
    "quote_type": "EQUITY",
}


class UpcomingTests(unittest.TestCase):
    def test_quarter_dip_stays_a_strong_buy(self):
        row = judge_upcoming(
            [15e9, 12e9, 11e9, 9e9, 8e9],
            [2.0e9, 4.5e9, 1.2e9, 6e9],
            quality=WMT,
            quarter_labels=["Q3 2026", "Q2 2026", "Q1 2026", "Q4 2025"],
        )
        self.assertTrue(row["included"])
        self.assertEqual(row["rating"], "HIGH BUY")
        self.assertTrue(row["flagged"])
        self.assertAlmostEqual(row["fcf_change"], -2.5e9)
        self.assertIn("$2.5B", row["flag"])
        self.assertIn("Q2 2026", row["flag"])
        self.assertIn("Q3 2026", row["flag"])
        self.assertIn("Strong buy", row["why"])
        self.assertIn("stays a strong buy", row["why"])

    def test_negative_quarter_is_kept_when_the_annual_pace_holds(self):
        row = judge_upcoming(
            [15e9, 12e9, 11e9, 9e9],
            [-1e9, 3e9],
            quality={"market_cap": 200e9, "profit_margin": 0.08, "roe": 0.15, "quote_type": "EQUITY"},
        )
        self.assertTrue(row["included"])
        self.assertTrue(row["flagged"])
        self.assertEqual(row["rating"], "HIGH BUY")
        self.assertIn("$4.0B", row["flag"])
        self.assertIn("turned negative", row["why"])

    def test_steady_rise_has_no_flag(self):
        row = judge_upcoming(
            [10e9, 8e9, 7e9, 6e9],
            [3e9, 2e9],
            quality={"market_cap": 80e9, "profit_margin": 0.2, "roe": 0.2, "quote_type": "EQUITY"},
        )
        self.assertTrue(row["included"])
        self.assertFalse(row["flagged"])
        self.assertGreater(row["pace"], 0)

    def test_one_softer_year_still_counts_as_steady(self):
        row = judge_upcoming(
            [12e9, 14e9, 10e9, 8e9],
            [2e9, 2e9],
            quality={"market_cap": 80e9, "profit_margin": 0.12, "roe": 0.16, "quote_type": "EQUITY"},
        )
        self.assertTrue(row["included"])

    def test_falling_annual_cash_flow_is_off_the_list(self):
        row = judge_upcoming(
            [4e9, 6e9, 8e9, 10e9],
            [1e9, 2e9],
            quality={"market_cap": 80e9, "profit_margin": 0.2, "roe": 0.2, "quote_type": "EQUITY"},
        )
        self.assertFalse(row["included"])

    def test_fund_is_off_the_list(self):
        row = judge_upcoming(
            [10e9, 8e9, 7e9, 6e9],
            [3e9, 2e9],
            quality={"ticker": "SPY", "market_cap": 500e9, "profit_margin": 0.2, "roe": 0.2, "quote_type": "ETF"},
        )
        self.assertFalse(row["included"])


if __name__ == "__main__":
    unittest.main()
