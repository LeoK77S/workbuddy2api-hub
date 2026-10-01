"""The pricing engine turns usage rows into official-API-equivalent CNY.

Pinned here: the peak/off-peak schedule (Beijing time, legal holidays
excluded), the cache-hit/miss split, the USD conversion at the snapshot
rate, and the "unknown model" contract (known=False - never a guessed
number). The schedule tests build their timestamps from UTC rather than
the host clock, so they hold in any timezone.

No network: the snapshot ships inline with the module.
"""
import calendar
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-price-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_pricing
import wb_proxy as P


def bj_ts(date_text, hour, minute=0):
    """Epoch for a Beijing-time wall clock, independent of the host zone."""
    base = calendar.timegm(time.strptime(date_text, "%Y-%m-%d"))
    return base + hour * 3600 + minute * 60 - 8 * 3600


class PricingEngineTests(unittest.TestCase):
    def setUp(self):
        self.pricing = wb_pricing.load_pricing()
        self.holidays = self.pricing["meta"]["holidays"]

    def test_snapshot_loads_and_carries_the_rate(self):
        self.assertIn("models", self.pricing)
        self.assertGreater(len(self.pricing["models"]), 10)
        self.assertGreater(wb_pricing.usd_cny(), 1.0)

    def test_peak_window_is_beijing_weekday_hours(self):
        # 2026-09-30 is a Wednesday.
        self.assertTrue(wb_pricing.is_peak(bj_ts("2026-09-30", 9), self.holidays))
        self.assertTrue(wb_pricing.is_peak(bj_ts("2026-09-30", 11, 59), self.holidays))
        self.assertFalse(wb_pricing.is_peak(bj_ts("2026-09-30", 12), self.holidays))
        self.assertTrue(wb_pricing.is_peak(bj_ts("2026-09-30", 14), self.holidays))
        self.assertFalse(wb_pricing.is_peak(bj_ts("2026-09-30", 18), self.holidays))
        self.assertFalse(wb_pricing.is_peak(bj_ts("2026-09-30", 20), self.holidays))
        # Weekends are off-peak all day.
        self.assertFalse(wb_pricing.is_peak(bj_ts("2026-10-03", 10), self.holidays))
        # A legal holiday counts as off-peak even on a weekday hour
        # (2026-10-01 is National Day).
        self.assertFalse(wb_pricing.is_peak(bj_ts("2026-10-01", 10), self.holidays))

    def test_deepseek_prices_by_schedule_and_cache_split(self):
        row = {"model": "deepseek-v4.1-flash", "at": bj_ts("2026-09-30", 10),
               "prompt_tokens": 1000000, "cached_tokens": 0,
               "completion_tokens": 0}
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertTrue(got["known"])
        self.assertTrue(got["peak"])
        # 1M cache-miss input at the peak miss rate (2.0/1M).
        self.assertAlmostEqual(got["cny"], 2.0, places=6)
        # 800k of the input hit the cache (0.04/1M): 0.8*0.04 + 0.2*2.0.
        row["cached_tokens"] = 800000
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertAlmostEqual(got["cny"], 0.432, places=6)
        # Off-peak (20:00): miss 1.0 + output 4.0 per 1M.
        row2 = {"model": "deepseek-v4.1-flash", "at": bj_ts("2026-09-30", 20),
                "prompt_tokens": 1000000, "cached_tokens": 0,
                "completion_tokens": 1000000}
        got = wb_pricing.compute_row(row2, pricing=self.pricing)
        self.assertFalse(got["peak"])
        self.assertAlmostEqual(got["cny"], 5.0, places=6)

    def test_usd_entries_convert_at_the_snapshot_rate(self):
        # gpt-6-astra rides the OpenRouter USD table: 10/1M miss.
        row = {"model": "gpt-6-astra", "at": bj_ts("2026-09-30", 10),
               "prompt_tokens": 1000000, "cached_tokens": 0,
               "completion_tokens": 0}
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertTrue(got["known"])
        # Flat-rate entries carry no schedule state.
        self.assertIsNone(got["peak"])
        self.assertAlmostEqual(
            got["cny"], 10.0 * self.pricing["meta"]["usd_cny"], places=4)

    def test_unknown_model_is_reported_not_guessed(self):
        got = wb_pricing.compute_row(
            {"model": "no-such-model", "at": bj_ts("2026-09-30", 10),
             "prompt_tokens": 1000000}, pricing=self.pricing)
        self.assertFalse(got["known"])
        self.assertEqual(got["cny"], 0.0)
        total = wb_pricing.sum_rows([
            {"model": "no-such-model", "at": bj_ts("2026-09-30", 10),
             "prompt_tokens": 1},
            {"model": "gpt-6-astra", "at": bj_ts("2026-09-30", 10),
             "prompt_tokens": 1000000},
        ], pricing=self.pricing)
        self.assertEqual(total["missing"], {"no-such-model": 1})
        self.assertEqual(total["priced"], 1)
        self.assertAlmostEqual(
            total["cny"], 10.0 * self.pricing["meta"]["usd_cny"], places=4)

    def test_dirty_cache_counts_cannot_exceed_prompt(self):
        row = {"model": "deepseek-v4.1-flash", "at": bj_ts("2026-09-30", 10),
               "prompt_tokens": 1000, "cached_tokens": 5000,
               "completion_tokens": 0}
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        # The whole 1000 is billed at the hit rate; the extra "cached" is
        # clamped away instead of producing negative miss tokens.
        self.assertAlmostEqual(got["cny"], 1000 * 0.04 / 1000000, places=9)


class PricingIntegrationTests(unittest.TestCase):
    """The usage readers attach the cost to the rows they already serve."""

    def setUp(self):
        with P._daily_usage_lock:
            P._daily_usage.update({"day": "", "totals": None, "credits": None,
                                   "models": None, "offset": 0, "at": 0.0})
        row = {"at": time.time() - 120, "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "model": "gpt-6-astra", "stream": True, "outcome": "completed",
               "elapsed_ms": 1200, "ttft_ms": 300, "gen_ms": 900,
               "prompt_tokens": 1000000, "completion_tokens": 0,
               "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 1000000,
               "credit": 1.5, "account": "uid-cost", "realm": "intl"}
        with open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.write(json.dumps(dict(row, model="no-such-model", credit=0),
                                ensure_ascii=False) + "\n")

    def test_recent_rows_carry_the_estimate_and_the_rate(self):
        page = P.recent_usage(limit=10)
        self.assertGreater(page["usd_cny"], 1.0)
        by_model = {r["model"]: r for r in page["rows"]}
        expected = 10.0 * page["usd_cny"]
        self.assertAlmostEqual(by_model["gpt-6-astra"]["cost_cny"], expected,
                               places=4)
        # An unpriced model answer stays None instead of a fake zero.
        self.assertIsNone(by_model["no-such-model"]["cost_cny"])

    def test_snapshot_totals_include_cost_and_missing(self):
        snap = P.usage_snapshot(ttl=0, range="all")
        self.assertAlmostEqual(snap["cost_cny"], 10.0 * snap["usd_cny"], places=3)
        self.assertEqual(snap["cost_missing"], {"no-such-model": 1})
        per = snap["by_model"]["gpt-6-astra"]
        self.assertAlmostEqual(per["cost_cny"], 10.0 * snap["usd_cny"], places=3)


if __name__ == "__main__":
    unittest.main()
