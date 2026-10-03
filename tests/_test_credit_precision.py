"""Credits are two-decimal quantities, and the readers must not leak more.

Upstream reports each request's credit as 0.05, 0.13 and so on (measured over
a production log: every one of 18355 rows carried two decimals), but the
gateway sums them as floats, where 0.1 + 0.2 is 0.30000000000000004. Every
credit figure therefore leaves the process rounded to the cent. This pins that
boundary: a total with seventeen digits is what the panel and any API client
would otherwise print, and no amount of front-end formatting fixes the payload
itself.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="wb-credit-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP
os.makedirs(os.environ["ACCOUNTS_DIR"], exist_ok=True)

import wb_proxy as P


def row(credit, model="gpt-6-astra", account="uid-a", at=None):
    return {
        "at": at if at is not None else time.time() - 60,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model, "stream": True, "outcome": "completed",
        "elapsed_ms": 1000, "ttft_ms": 200, "gen_ms": 800,
        "prompt_tokens": 1000, "completion_tokens": 100,
        "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 1100,
        "credit": credit, "account": account, "realm": "intl",
    }


class CreditRoundingTests(unittest.TestCase):
    def test_the_helper_rounds_credit_fields_and_leaves_the_rest(self):
        node = {"credit": 0.1 + 0.2, "tokens": 1.23456789,
                "by_model": {"m": {"credit": 0.1 + 0.2}},
                "rows": [{"credit": 0.1 + 0.2, "other": 3.5}]}
        P._round_credits(node)
        self.assertEqual(node["credit"], 0.3)
        self.assertEqual(node["by_model"]["m"]["credit"], 0.3)
        self.assertEqual(node["rows"][0]["credit"], 0.3)
        # Only credits are touched: token counts and rates keep their digits.
        self.assertEqual(node["tokens"], 1.23456789)
        self.assertEqual(node["rows"][0]["other"], 3.5)

    def test_the_premise_holds(self):
        # If this ever stops being true the rest of the file is pointless.
        self.assertNotEqual(0.1 + 0.2, 0.3)


class CreditOutputTests(unittest.TestCase):
    def setUp(self):
        P._snap_cache.clear()
        P._analytics_cache.clear()
        with open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
            for r in (row(0.1, account="uid-a"), row(0.2, account="uid-b"),
                      row(0.13, model="kimi-k2.6", account="uid-a")):
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_the_snapshot_total_is_two_decimals(self):
        snap = P.usage_snapshot(ttl=0, range="all")
        # 0.1 + 0.2 + 0.13 = 0.43000000000000005 in floats.
        self.assertEqual(snap["credit"], 0.43)
        self.assertEqual(str(snap["credit"]), "0.43")

    def test_per_model_and_per_account_credits_are_two_decimals(self):
        snap = P.usage_snapshot(ttl=0, range="all")
        self.assertEqual(snap["by_model"]["gpt-6-astra"]["credit"], 0.3)
        self.assertEqual(snap["by_model"]["kimi-k2.6"]["credit"], 0.13)

    def test_the_analytics_payload_is_two_decimals(self):
        data = P.compute_usage_analytics(ttl=0)
        self.assertEqual(data["summary"]["all_time"]["credit"], 0.43)
        self.assertEqual(data["summary"]["window"]["credit"], 0.43)
        per_account = {a["uid"]: a["all_time"]["credit"] for a in data["accounts"]}
        self.assertEqual(per_account["uid-a"], 0.23)
        per_model = {m["model"]: m["all_time"]["credit"] for m in data["models"]}
        self.assertEqual(per_model["gpt-6-astra"], 0.3)

    def test_recent_rows_are_two_decimals(self):
        page = P.recent_usage(limit=10)
        credits = sorted(r["credit"] for r in page["rows"])
        self.assertEqual(credits, [0.1, 0.13, 0.2])
        for r in page["rows"]:
            self.assertEqual(r["credit"], round(float(r["credit"]), 2))


if __name__ == "__main__":
    unittest.main()
