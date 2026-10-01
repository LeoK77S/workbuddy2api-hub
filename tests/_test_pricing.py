"""The pricing engine turns usage rows into OpenRouter-equivalent CNY.

Pinned here: the cache-hit/miss split, the USD conversion at the snapshot
rate, the conditional bands OpenRouter prices some models by (an input-length
threshold, or a UTC window; of the bands that apply, the tightest wins), the
snapshot's shape (every entry USD, no per-vendor schedule), the "unknown
model" contract (known=False - never a guessed number), and the matcher that
keeps the snapshot current when the model catalogue gains an entry.

No network: the snapshot ships inline with the module, and the matcher tests
only exercise the pure lookup helpers.
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

import _fetch_pricing as FP
import wb_pricing
import wb_proxy as P


def utc_ts(text):
    """Epoch for a UTC wall clock - the source states its bands in UTC."""
    return calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


# A fixed instant for the rows that are not about time bands: if the snapshot
# ever gains a band covering it, the test should fail rather than flip with
# the wall clock.
_AT = utc_ts("2026-09-30 20:00:00")


class PricingEngineTests(unittest.TestCase):
    def setUp(self):
        self.pricing = wb_pricing.load_pricing()

    def test_snapshot_loads_and_carries_the_rate(self):
        self.assertIn("models", self.pricing)
        self.assertGreater(len(self.pricing["models"]), 10)
        self.assertGreater(wb_pricing.usd_cny(), 1.0)

    def test_every_entry_is_usd_with_a_base_price(self):
        # One source, one shape. The panel labels the number as OpenRouter's
        # price, so a leftover vendor schedule or a stray CNY entry would make
        # that label wrong.
        for mid, entry in self.pricing["models"].items():
            self.assertEqual(entry.get("currency"), "USD", mid)
            self.assertNotIn("schedule", entry, mid)
            self.assertNotIn("peak", entry, mid)
            self.assertIsInstance(entry.get("flat"), dict, mid)
            for key in ("input_cache_hit", "input_cache_miss", "output"):
                self.assertIn(key, entry["flat"], "%s.%s" % (mid, key))
            for band in entry.get("bands") or []:
                self.assertIsInstance(band.get("flat"), dict, mid)
                # A band carries one condition: a length threshold or a UTC
                # window, never both and never neither.
                if "min_prompt_tokens" in band:
                    self.assertGreater(band["min_prompt_tokens"], 0, mid)
                    self.assertNotIn("start", band, mid)
                else:
                    self.assertLess(band["start"], band["end"], mid)

    def test_snapshot_keeps_the_sources_conditional_bands(self):
        # These are priced conditionally upstream. If the bands were lost in
        # transit the estimate would silently revert to the base price for
        # exactly the models where that is wrong.
        for mid in ("hy4-preview-f", "hy3", "hy3-x", "gpt-6-astra",
                    "gpt-5.5", "grok-4.7"):
            entry = self.pricing["models"].get(mid)
            self.assertIsNotNone(entry, mid)
            self.assertIn("bands", entry, mid)

    def test_time_banded_model_picks_its_band(self):
        entry = self.pricing["models"]["hy4-preview-f"]
        bands = entry["bands"]
        factor = self.pricing["meta"]["usd_cny"]
        # The bands must really differ, or matching one of them proves nothing.
        self.assertNotEqual(bands[0]["flat"]["input_cache_miss"],
                            bands[1]["flat"]["input_cache_miss"])
        row = {"model": "hy4-preview-f", "prompt_tokens": 1000000,
               "cached_tokens": 0, "completion_tokens": 0}
        # 02:00 UTC lands in the first band, 17:00 UTC in the second.
        row["at"] = utc_ts("2026-09-30 02:00:00")
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertEqual(got["band"], 0)
        self.assertAlmostEqual(
            got["cny"], bands[0]["flat"]["input_cache_miss"] * factor, places=6)
        row["at"] = utc_ts("2026-09-30 17:00:00")
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertEqual(got["band"], 1)
        self.assertAlmostEqual(
            got["cny"], bands[1]["flat"]["input_cache_miss"] * factor, places=6)

    def test_band_lookup_is_by_utc_window(self):
        entry = {
            "flat": {"input_cache_miss": 1.0},
            "bands": [
                {"days": ["sat", "sun"], "start": 0, "end": 2400,
                 "flat": {"input_cache_miss": 2.0}},
                {"days": None, "start": 100, "end": 400,
                 "flat": {"input_cache_miss": 3.0}},
            ],
        }
        # Saturday 12:00 UTC: the weekend band, which sets no hour range.
        table, index = wb_pricing.band_for(entry, utc_ts("2026-10-03 12:00:00"))
        self.assertEqual(index, 0)
        self.assertEqual(table["input_cache_miss"], 2.0)
        # Wednesday 02:00 UTC falls inside [01:00, 04:00).
        self.assertEqual(
            wb_pricing.band_for(entry, utc_ts("2026-09-30 02:00:00"))[1], 1)
        # Wednesday 05:00 UTC matches no band, so the flat price stands in.
        self.assertIsNone(
            wb_pricing.band_for(entry, utc_ts("2026-09-30 05:00:00"))[1])

    def test_length_bands_take_the_tightest_match(self):
        # The source lists length bands in ascending order, so "the last one
        # that applies" is the tightest one, not the first that fits.
        entry = {"flat": {"input_cache_miss": 1.0}, "bands": [
            {"min_prompt_tokens": 32000, "flat": {"input_cache_miss": 2.0}},
            {"min_prompt_tokens": 128000, "flat": {"input_cache_miss": 3.0}},
        ]}
        table, index = wb_pricing.band_for(entry, _AT, 40000)
        self.assertEqual(index, 0)
        self.assertEqual(table["input_cache_miss"], 2.0)
        table, index = wb_pricing.band_for(entry, _AT, 128000)
        self.assertEqual(index, 1)
        self.assertEqual(table["input_cache_miss"], 3.0)
        table, index = wb_pricing.band_for(entry, _AT, 200000)
        self.assertEqual(index, 1)
        self.assertEqual(table["input_cache_miss"], 3.0)
        # Below the lowest threshold the base price stands in.
        self.assertIsNone(wb_pricing.band_for(entry, _AT, 1000)[1])

    def test_length_banded_model_picks_its_band(self):
        entry = self.pricing["models"]["gpt-6-astra"]
        bands = entry["bands"]
        factor = self.pricing["meta"]["usd_cny"]
        threshold = bands[0]["min_prompt_tokens"]
        base = {"model": "gpt-6-astra", "at": _AT, "cached_tokens": 0,
                "completion_tokens": 0}
        # Just under the threshold the model's own base price applies.
        below = threshold - 1
        got = wb_pricing.compute_row(dict(base, prompt_tokens=below),
                                     pricing=self.pricing)
        self.assertIsNone(got["band"])
        self.assertAlmostEqual(
            got["cny"],
            below * entry["flat"]["input_cache_miss"] / 1000000.0 * factor,
            places=6)
        # At the threshold the dearer band takes over.
        got = wb_pricing.compute_row(dict(base, prompt_tokens=threshold),
                                     pricing=self.pricing)
        self.assertEqual(got["band"], 0)
        self.assertAlmostEqual(
            got["cny"],
            threshold * bands[0]["flat"]["input_cache_miss"] / 1000000.0 * factor,
            places=6)
        self.assertNotEqual(entry["flat"]["input_cache_miss"],
                            bands[0]["flat"]["input_cache_miss"])

    def test_band_end_zero_means_end_of_day(self):
        # The source writes the closing edge of its last band as 0.
        entry = {"flat": {}, "bands": [
            {"days": None, "start": 1600, "end": 0,
             "flat": {"input_cache_miss": 5.0}}]}
        table, index = wb_pricing.band_for(entry, utc_ts("2026-09-30 20:00:00"))
        self.assertEqual(index, 0)
        self.assertEqual(table["input_cache_miss"], 5.0)
        self.assertIsNone(
            wb_pricing.band_for(entry, utc_ts("2026-09-30 10:00:00"))[1])

    def test_flat_model_reports_no_band(self):
        got = wb_pricing.compute_row(
            {"model": "deepseek-v4.1-flash", "at": _AT,
             "prompt_tokens": 1000000}, pricing=self.pricing)
        self.assertTrue(got["known"])
        self.assertIsNone(got["band"])

    def test_cache_split_and_usd_conversion(self):
        rate = self.pricing["models"]["deepseek-v4.1-flash"]["flat"]
        factor = self.pricing["meta"]["usd_cny"]
        row = {"model": "deepseek-v4.1-flash", "at": _AT,
               "prompt_tokens": 1000000, "cached_tokens": 0,
               "completion_tokens": 0}
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertTrue(got["known"])
        # 1M cache-miss input at the snapshot's miss rate, in CNY.
        self.assertAlmostEqual(
            got["cny"], rate["input_cache_miss"] * factor, places=6)
        # 800k of the input hit the cache and is billed at the hit rate.
        row["cached_tokens"] = 800000
        expected = (0.2 * rate["input_cache_miss"]
                    + 0.8 * rate["input_cache_hit"]) * factor
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertAlmostEqual(got["cny"], expected, places=6)
        # Output tokens are priced separately, at the output rate.
        row["completion_tokens"] = 1000000
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        self.assertAlmostEqual(got["cny"], expected + rate["output"] * factor,
                               places=6)

    def test_unknown_model_is_reported_not_guessed(self):
        got = wb_pricing.compute_row(
            {"model": "no-such-model", "at": _AT,
             "prompt_tokens": 1000000}, pricing=self.pricing)
        self.assertFalse(got["known"])
        self.assertEqual(got["cny"], 0.0)
        total = wb_pricing.sum_rows([
            {"model": "no-such-model", "at": _AT, "prompt_tokens": 1},
            {"model": "deepseek-v4.1-flash", "at": _AT,
             "prompt_tokens": 1000000},
        ], pricing=self.pricing)
        self.assertEqual(total["missing"], {"no-such-model": 1})
        self.assertEqual(total["priced"], 1)
        rate = self.pricing["models"]["deepseek-v4.1-flash"]["flat"]
        self.assertAlmostEqual(
            total["cny"], rate["input_cache_miss"] * self.pricing["meta"]["usd_cny"],
            places=4)

    def test_dirty_cache_counts_cannot_exceed_prompt(self):
        row = {"model": "deepseek-v4.1-flash", "at": _AT,
               "prompt_tokens": 1000, "cached_tokens": 5000,
               "completion_tokens": 0}
        got = wb_pricing.compute_row(row, pricing=self.pricing)
        # The whole 1000 is billed at the hit rate; the extra "cached" is
        # clamped away instead of producing negative miss tokens.
        rate = self.pricing["models"]["deepseek-v4.1-flash"]["flat"]
        self.assertAlmostEqual(
            got["cny"], 1000 * rate["input_cache_hit"] / 1000000
            * self.pricing["meta"]["usd_cny"], places=9)


class PricingMatcherTests(unittest.TestCase):
    """The fetcher must price a newly added catalogue model without an edit.

    These exercise the pure helpers only - nothing here touches the network.
    """

    def test_normalize_ignores_case_and_punctuation(self):
        self.assertEqual(FP.normalize("hy4-preview-f"),
                         FP.normalize("HY4.Preview_F"))
        self.assertEqual(FP.normalize(None), "")

    def test_auto_match_wants_an_exact_unique_name(self):
        by_norm = FP.index_openrouter({
            "vendor/some-model": {},
            "other/another": {},
        })
        self.assertEqual(FP.auto_match("some-model", by_norm), "vendor/some-model")
        self.assertEqual(FP.auto_match("Some.Model", by_norm), "vendor/some-model")
        # Two vendors publishing the same leaf name is too ambiguous to guess.
        ambiguous = FP.index_openrouter({"a/dup": {}, "b/dup": {}})
        self.assertIsNone(FP.auto_match("dup", ambiguous))
        self.assertIsNone(FP.auto_match("no-such-model", by_norm))

    def test_index_drops_variant_suffixes(self):
        # :free / :batch are separate products with their own prices; keeping
        # them would make an equally-named base model ambiguous.
        self.assertEqual(
            FP.index_openrouter({"vendor/m:free": {}, "vendor/m:batch": {}}), {})

    def test_resolve_prefers_the_override_table(self):
        or_models = {"tencent/hy4-preview": {"prompt": "0.000001"}}
        by_norm = FP.index_openrouter(or_models)
        self.assertEqual(FP.resolve("hy4-preview-f", or_models, by_norm),
                         ("tencent/hy4-preview", True))
        # An override that is missing from the fetched list falls through to
        # "unpriced" rather than pointing at a non-existent entry.
        self.assertEqual(FP.resolve("hy4-preview-f", {}, {}), (None, False))

    def test_overrides_point_at_openrouter_shaped_ids(self):
        # A typo in the override table silently unprices the model.
        for hub_id, ref in FP.OVERRIDES.items():
            self.assertIn("/", ref, hub_id)
            self.assertNotIn(":", ref, hub_id)

    def test_bands_cover_both_kinds_of_condition(self):
        bands = FP.bands_from_overrides({"overrides": [
            {"min_prompt_tokens": 200000, "prompt": "0.000006",
             "completion": "0.0000225"},
            {"utc_start": 0, "utc_end": 1600, "prompt": "0.000000834",
             "completion": "0.000002501", "input_cache_read": "0.000000042"},
            {"utc_days": ["monday", "friday"], "utc_start": 1600, "utc_end": 0,
             "prompt": "0.0000007506", "completion": "0.0000022509",
             "input_cache_read": "0.0000000378"},
        ]})
        self.assertEqual(len(bands), 3)
        # A length band carries the threshold and no window.
        self.assertEqual(bands[0]["min_prompt_tokens"], 200000)
        self.assertNotIn("start", bands[0])
        self.assertEqual(bands[0]["flat"]["input_cache_miss"], 6.0)
        # A window band carries the window and no threshold.
        self.assertNotIn("min_prompt_tokens", bands[1])
        self.assertIsNone(bands[1]["days"])
        self.assertEqual((bands[1]["start"], bands[1]["end"]), (0, 1600))
        self.assertEqual(bands[1]["flat"]["input_cache_miss"], 0.834)
        self.assertEqual(bands[1]["flat"]["input_cache_hit"], 0.042)
        # A closing edge of 0 means the end of the day, and weekday names are
        # stored in the short form the engine compares against.
        self.assertEqual(bands[2]["days"], ["mon", "fri"])
        self.assertEqual((bands[2]["start"], bands[2]["end"]), (1600, 2400))

    def test_bands_need_a_condition(self):
        self.assertEqual(FP.bands_from_overrides({}), [])
        # An entry carrying no condition repeats the base price: not a band.
        self.assertEqual(FP.bands_from_overrides({"overrides": [
            {"prompt": "0.00001", "completion": "0.00005"}]}), [])
        # The audio/cache-write axes have no token count in the local usage
        # log, so an entry priced only on them is not a band either.
        self.assertEqual(FP.bands_from_overrides({"overrides": [
            {"audio": "0.00002", "input_audio_cache": "0.000004"}]}), [])

    def test_snapshot_reflects_the_matchers_choice(self):
        # hy4-preview-f only exists under a different name upstream, so it
        # must appear in the snapshot through the override.
        entry = self._snapshot_entry("hy4-preview-f")
        self.assertEqual(entry["or_id"], FP.OVERRIDES["hy4-preview-f"])
        self.assertEqual(entry["source"], "openrouter")
        # A model the catalogue knows and OpenRouter names identically is
        # matched automatically, with no override behind it.
        self.assertNotIn("kimi-k2.6", FP.OVERRIDES)
        auto = self._snapshot_entry("kimi-k2.6")
        self.assertEqual(auto["or_id"], "moonshotai/kimi-k2.6")

    def test_catalogue_ids_are_unique(self):
        ids = FP.hub_model_ids()
        self.assertIn("hy4-preview-f", ids)
        self.assertEqual(len(ids), len(set(ids)))

    def _snapshot_entry(self, mid):
        entry = wb_pricing.load_pricing()["models"].get(mid)
        self.assertIsNotNone(entry, "snapshot has no entry for %s" % mid)
        return entry


class PricingIntegrationTests(unittest.TestCase):
    """The usage readers attach the cost to the rows they already serve."""

    # A model with one flat price: the point here is the plumbing, not the
    # band lookup, which the engine tests cover on the models that have bands.
    MODEL = "deepseek-v4.1-flash"

    def setUp(self):
        with P._daily_usage_lock:
            P._daily_usage.update({"day": "", "totals": None, "credits": None,
                                   "models": None, "offset": 0, "at": 0.0})
        self.usd_rate = wb_pricing.load_pricing()["models"][self.MODEL]["flat"][
            "input_cache_miss"]
        row = {"at": time.time() - 120, "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "model": self.MODEL, "stream": True, "outcome": "completed",
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
        expected = self.usd_rate * page["usd_cny"]
        self.assertAlmostEqual(by_model[self.MODEL]["cost_cny"], expected,
                               places=4)
        # One flat price means no band, and the key is still present.
        self.assertIsNone(by_model[self.MODEL]["cost_band"])
        # An unpriced model answer stays None instead of a fake zero.
        self.assertIsNone(by_model["no-such-model"]["cost_cny"])

    def test_snapshot_totals_include_cost_and_missing(self):
        snap = P.usage_snapshot(ttl=0, range="all")
        self.assertAlmostEqual(snap["cost_cny"], self.usd_rate * snap["usd_cny"],
                               places=3)
        self.assertEqual(snap["cost_missing"], {"no-such-model": 1})
        per = snap["by_model"][self.MODEL]
        self.assertAlmostEqual(per["cost_cny"], self.usd_rate * snap["usd_cny"],
                               places=3)


if __name__ == "__main__":
    unittest.main()
