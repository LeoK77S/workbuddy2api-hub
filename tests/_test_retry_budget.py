"""重试链的时间预算、首字节分级与客户端断连感知。

Run with: python _test_retry_budget.py
No upstream credentials or outbound network are used.

三条行为的边界都在这里钉住：
  - 总预算：链花完预算就停，429 仍然是 429，其余回 504 + Retry-After；
  - 首字节分级：第一次尝试用更短的响应头超时，给第二次留余量；
  - 断连感知：客户端走了就不再换号。
"""
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

_startup_dir = tempfile.TemporaryDirectory(prefix="retry-budget-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy
import wb_settings


def http_error(code, body=b""):
    return urllib.error.HTTPError("http://upstream.invalid/v2/chat/completions",
                                  code, "err", {}, io.BytesIO(body))


class StubResponse(object):
    """最小可用的上游响应：urlopen 的返回值只需要能关闭。"""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def make_account(directory, uid="uid-budget", realm="intl"):
    return wb_accounts.Account(
        {"uid": uid, "accessToken": "t", "realm": realm},
        os.path.join(directory, uid + ".json"))


class Pool(object):
    """pick_for_session 无视 exclude：测试要能反复挑到同一个账号，好观察第二次
    尝试的超时值。"""

    def __init__(self, account):
        self.accounts = [account]
        self.affinity = type("Affinity", (), {
            "unbind": lambda self, key: None,
            "demote": lambda self, key, uid=None: None})()

    def count_ready(self, realm, model=None):
        return len(self.accounts)

    def pick_for_session(self, realm, session_key=None, exclude=(), model=None, page=None):
        return self.accounts[0]

    def list_public(self):
        return []

    def apply_daily_token_limit(self, *args, **kwargs):
        return 0

    def apply_daily_credit_limit(self, *args, **kwargs):
        return 0

    def apply_model_daily_token_limit(self, *args, **kwargs):
        return 0

    def apply_remaining_weights(self, weights=None):
        return weights or {}


class ChainCase(unittest.TestCase):
    """把 open_upstream 挂在一个假池 + 假 urlopen 上跑。"""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="retry-budget-case-")
        self.calls = []
        self.account = make_account(self.directory)
        self.old_pool = wb_proxy.POOL
        self.old_urlopen = wb_accounts.urlopen
        wb_proxy.POOL = Pool(self.account)
        self.old_accounts_dir = wb_proxy.ACCOUNTS_DIR
        self.accounts_dir_patch = mock.patch.object(
            wb_proxy, "ACCOUNTS_DIR", self.directory)
        self.accounts_dir_patch.start()
        wb_proxy._CLIENT_SOCKET.sock = None

    def tearDown(self):
        wb_proxy.POOL = self.old_pool
        wb_accounts.urlopen = self.old_urlopen
        self.accounts_dir_patch.stop()
        wb_proxy._CLIENT_SOCKET.sock = None

    def configure(self, **upstream):
        wb_settings.set_upstream_config(self.directory, upstream)

    def stub(self, behaviour):
        """behaviour(req, timeout, proxy) -> 响应或抛异常；每次调用记下超时。"""

        def fake_urlopen(req, timeout=30, proxy=""):
            self.calls.append(timeout)
            return behaviour(req, timeout, proxy)

        wb_accounts.urlopen = fake_urlopen

    def chain(self):
        return wb_proxy.open_upstream(
            {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}]},
            target_realm="intl")


class BudgetTests(ChainCase):
    def test_defaults_are_off_and_explicit_values_are_honoured(self):
        # 仓库约定：会改变默认行为的改动默认关，所以两个值出厂都是 0。
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", directory):
                self.assertEqual(wb_proxy.retry_chain_budget(), (0.0, 0.0))
                wb_settings.set_upstream_config(directory, {
                    "retry_budget_seconds": 40, "first_attempt_timeout_seconds": 15})
                self.assertEqual(wb_proxy.retry_chain_budget(), (40.0, 15.0))

    def test_first_attempt_gets_the_shorter_deadline(self):
        self.configure(header_timeout_seconds=25, retry_budget_seconds=0,
                       first_attempt_timeout_seconds=7)
        self.stub(lambda req, timeout, proxy: (_ for _ in ()).throw(http_error(500)))
        with self.assertRaises(urllib.error.HTTPError):
            self.chain()
        # 第一次 7s，之后回到 header_timeout；预算为 0 时链的形状与旧版一致。
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], 7.0)
        self.assertEqual(self.calls[1], 25.0)

    def test_zero_first_attempt_keeps_every_attempt_equal(self):
        self.configure(header_timeout_seconds=25, retry_budget_seconds=0,
                       first_attempt_timeout_seconds=0)
        self.stub(lambda req, timeout, proxy: (_ for _ in ()).throw(http_error(500)))
        with self.assertRaises(urllib.error.HTTPError):
            self.chain()
        self.assertEqual(set(self.calls), {25.0})

    def test_budget_stops_the_chain_and_raises_504(self):
        self.configure(header_timeout_seconds=25, retry_budget_seconds=1,
                       first_attempt_timeout_seconds=15)
        self.stub(lambda req, timeout, proxy: (_ for _ in ()).throw(
            socket.timeout("timed out")))
        started = time.monotonic()
        with self.assertRaises(wb_proxy.RetryBudgetExhausted) as caught:
            self.chain()
        elapsed = time.monotonic() - started
        exc = caught.exception
        self.assertEqual(exc.code, 504)
        self.assertEqual(exc.attempts, 1)
        self.assertEqual(len(self.calls), 1, "预算花完就不该再开新尝试")
        self.assertLess(elapsed, 5.0, "链的时长不该超过预算太多")
        self.assertIn("budget", exc.detail)

    def test_single_attempt_never_outlives_the_budget(self):
        self.configure(header_timeout_seconds=600, retry_budget_seconds=2,
                       first_attempt_timeout_seconds=0)
        self.stub(lambda req, timeout, proxy: (_ for _ in ()).throw(
            socket.timeout("timed out")))
        with self.assertRaises(wb_proxy.RetryBudgetExhausted):
            self.chain()
        self.assertLessEqual(self.calls[0], 2.0, "单次尝试的超时要被预算夹住")
        self.assertGreater(self.calls[0], 1.5)

    def test_budget_keeps_a_429_a_429(self):
        self.configure(header_timeout_seconds=25, retry_budget_seconds=1,
                       first_attempt_timeout_seconds=15)
        self.stub(lambda req, timeout, proxy: (_ for _ in ()).throw(
            http_error(429, b'{"code":14003,"msg":"too many requests"}')))
        with self.assertRaises(wb_proxy.RateLimited) as caught:
            self.chain()
        self.assertGreaterEqual(caught.exception.wait, 1)

    def test_budget_zero_keeps_the_unbounded_shape(self):
        self.configure(header_timeout_seconds=25, retry_budget_seconds=0,
                       first_attempt_timeout_seconds=0)
        seen = {"n": 0}

        def behaviour(req, timeout, proxy):
            seen["n"] += 1
            raise http_error(500)

        self.stub(behaviour)
        with self.assertRaises(urllib.error.HTTPError):
            self.chain()
        # 旧路径：不按时间收手，只按尝试次数收手（max_attempts）。
        self.assertGreater(seen["n"], 2)


class ClientGoneTests(unittest.TestCase):
    def test_probe_detects_a_closed_peer(self):
        a, b = socket.socketpair()
        try:
            self.assertFalse(wb_proxy.client_gone(a))
            b.close()
            self.assertTrue(wb_proxy.client_gone(a))
        finally:
            a.close()

    def test_probe_ignores_pending_data(self):
        a, b = socket.socketpair()
        try:
            b.sendall(b"GET / HTTP/1.1\r\n")
            self.assertFalse(wb_proxy.client_gone(a), "有待读数据不等于客户端走了")
        finally:
            a.close()
            b.close()

    def test_probe_degrades_when_there_is_no_socket(self):
        wb_proxy._CLIENT_SOCKET.sock = None
        self.assertFalse(wb_proxy.client_gone())
        self.assertFalse(wb_proxy.client_gone(object()))

    def test_chain_aborts_before_the_first_attempt_when_the_client_left(self):
        directory = tempfile.mkdtemp(prefix="retry-budget-gone-")
        account = make_account(directory)
        calls = []
        old_pool, old_urlopen = wb_proxy.POOL, wb_accounts.urlopen
        a, b = socket.socketpair()
        b.close()
        wb_proxy.POOL = Pool(account)
        wb_accounts.urlopen = lambda req, timeout=30, proxy="": calls.append(timeout)
        wb_proxy._CLIENT_SOCKET.sock = a
        try:
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", directory):
                with self.assertRaises(wb_proxy.ClientDisconnected):
                    wb_proxy.open_upstream(
                        {"model": "deepseek-v4.1-flash",
                         "messages": [{"role": "user", "content": "hi"}]},
                        target_realm="intl")
            self.assertEqual(calls, [], "客户端已经走了就不该再打上游")
        finally:
            wb_proxy.POOL = old_pool
            wb_accounts.urlopen = old_urlopen
            wb_proxy._CLIENT_SOCKET.sock = None
            a.close()

    def test_probe_survives_a_broken_socket_object(self):
        class Broken(object):
            def fileno(self):
                raise OSError("closed")

        self.assertTrue(wb_proxy.client_gone(Broken()))


class ResponderTests(unittest.TestCase):
    """504 响应面：状态码、Retry-After 与错误体。"""

    class FakeHandler(object):
        def __init__(self):
            self.status = None
            self.headers = []
            self.wfile = io.BytesIO()
            self.path = "/v1/chat/completions"
            self.drained = False

        def _handle_expect_continue(self):
            pass

        def _discard_body(self):
            self.drained = True

        def send_response(self, code):
            self.status = code

        def send_header(self, key, value):
            self.headers.append((key, value))

        def end_headers(self):
            pass

    def test_openai_504_carries_retry_after(self):
        handler = self.FakeHandler()
        exc = wb_proxy.RetryBudgetExhausted("retry budget 40s spent after 2 attempt(s)",
                                            wait=15, attempts=2, elapsed=40.0)
        wb_proxy.Handler._retry_budget_exhausted(handler, exc)
        self.assertEqual(handler.status, 504)
        self.assertIn(("Retry-After", "15"), handler.headers)
        self.assertTrue(handler.drained)
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(body["error"]["code"], 504)
        self.assertEqual(body["error"]["retry_after"], 15)
        self.assertIn("retry budget", body["error"]["message"])

    def test_anthropic_504_uses_the_anthropic_envelope(self):
        handler = self.FakeHandler()
        handler.path = "/v1/messages"
        exc = wb_proxy.RetryBudgetExhausted("budget", wait=15, attempts=1, elapsed=40.0)
        wb_proxy.Handler._anthropic_retry_budget_exhausted(handler, exc)
        self.assertEqual(handler.status, 504)
        self.assertIn(("Retry-After", "15"), handler.headers)
        body = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertIn("error", body)


class SettingsTests(unittest.TestCase):
    def test_defaults_include_the_budget_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = wb_settings.upstream_config(directory)
        self.assertEqual(cfg["retry_budget_seconds"], 0)
        self.assertEqual(cfg["first_attempt_timeout_seconds"], 0)

    def test_validate_accepts_zero_and_rejects_junk(self):
        self.assertEqual(wb_settings.validate_upstream_patch({"retry_budget_seconds": 0}),
                         {"retry_budget_seconds": 0})
        self.assertEqual(
            wb_settings.validate_upstream_patch({"first_attempt_timeout_seconds": 0}),
            {"first_attempt_timeout_seconds": 0})
        for bad in ({"retry_budget_seconds": True},
                    {"retry_budget_seconds": "40"},
                    {"retry_budget_seconds": -1},
                    {"retry_budget_seconds": 3601},
                    {"first_attempt_timeout_seconds": 1.5}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb_settings.validate_upstream_patch(bad)

    def test_lenient_load_falls_back_on_corrupt_values(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(wb_settings.settings_path(directory), "w", encoding="utf-8") as fh:
                json.dump({"upstream": {"retry_budget_seconds": "nope",
                                        "first_attempt_timeout_seconds": -5}}, fh)
            cfg = wb_settings.upstream_config(directory)
        self.assertEqual(cfg["retry_budget_seconds"], 0)
        self.assertEqual(cfg["first_attempt_timeout_seconds"], 0)

    def test_runtime_settings_view_exposes_the_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(wb_proxy, "ACCOUNTS_DIR", directory):
                view = wb_proxy.runtime_settings_view()
        self.assertEqual(view["upstream"]["retry_budget_seconds"], 0)
        self.assertEqual(view["upstream"]["first_attempt_timeout_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
