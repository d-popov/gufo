#!/usr/bin/env python3
"""CPU-only tests for tools/monitor/billing: pricing, ledger, metering, key sync, the
portal API and the monitor's admin account routes. Chains are faked; wallet signatures
use PyNaCl as an independent Ed25519 signer."""

import contextlib
import io
import base64
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/monitor"))
SPEC = importlib.util.spec_from_file_location("gufo_monitor", ROOT / "tools/monitor/gufo-monitor.py")
gm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gm)

import billing  # noqa: E402
from billing import chain, crypto, db as bdb, portal  # noqa: E402
from billing.pricing import MAX_MODELS, MAX_PRICE, PriceList, Pricing, PricingError  # noqa: E402

ADMIN_TOKEN = "admin-token-for-tests"
OWNER_LINE = "owner sha256:" + "ab" * 32
MODEL = "qwen3.8-27b"
LINE = (
    "2026-10-23 20:12:{s:02d} [INFO] [http] request={request} event=completed method=POST "
    "path=/v1/chat/completions status=200 duration_ms=329.1 outcome=completed "
    "prompt_tokens=1000 prefill_tokens=800 generated_tokens=100 finish=stop cache=memory "
    "cached_tokens=200 cache_restore_ms=0.0 queue_ms=1.5 ttft_ms=246.4 key={key} "
    "rss_mib=1234 host_available_mib=90000"
)
# 800 prefill * $1 + 200 cached * $0.10 + 100 output * $3 per 1M tokens, in micro-USD.
LINE_COST = 800 + 20 + 300


def log_line(key: str, request: str = "r1", second: int = 0) -> str:
    return LINE.format(s=second, request=request, key=key)


class NoTelemetry:
    def read(self):
        return {}


class FakeChain:
    """Deposit checker with canned answers, per transaction id."""

    def __init__(self):
        self.answers = {}
        self.accounts = {}
        self.blockhash = crypto.b58encode(bytes(range(1, 33)))

    def check(self, tx):
        return self.answers.get(tx, chain.DepositCheck("pending", None, 0, "not found"))

    def token_accounts(self, owner):
        return self.accounts.get(owner, [])

    def latest_blockhash(self):
        return self.blockhash


def serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class PricingTest(unittest.TestCase):
    def test_cost_rounds_up_and_bills_cached_tokens_once(self):
        pricing = Pricing.parse({"default": {"input": 1_000_000, "cached_input": 100_000,
                                             "output": 3_000_000}})
        self.assertEqual(pricing.cost(None, 1000, 200, 100), LINE_COST)
        self.assertEqual(pricing.cost(None, 1, 0, 0), 1)  # 1e6 / 1e6
        self.assertEqual(pricing.cost(None, 0, 0, 1), 3)
        self.assertEqual(pricing.cost(None, 1, 1, 0), 1)  # 0.1 rounds up
        self.assertEqual(pricing.cost(None, 10, 50, 0), 1)  # cached clamps to prompt
        self.assertEqual(pricing.cost(None, 0, 0, 0), 0)

    def test_model_prices_and_validation(self):
        pricing = Pricing.parse({"default": {"input": 1, "cached_input": 1, "output": 1},
                                 "models": {MODEL: {"input": 2_000_000, "cached_input": 0,
                                                    "output": 0}}})
        self.assertEqual(pricing.cost(MODEL, 10, 0, 0), 20)
        self.assertEqual(pricing.cost("other", 1_000_000, 0, 0), 1)
        with self.assertRaises(PricingError):
            Pricing.parse({"default": {"input": 1.5, "cached_input": 0, "output": 0}})
        with self.assertRaises(PricingError):
            Pricing.parse({"default": {"input": 1, "cached_input": 1, "output": 1},
                           "models": {"m": {"input": -1, "cached_input": 0, "output": 0}}})

    def test_example_files_load(self):
        Pricing.load(ROOT / "tools/monitor/pricing.example.json")
        config = billing.load_config(ROOT / "tools/monitor/billing.example.json")
        self.assertEqual(config["signup_grant_micro"], 990_000)
        self.assertEqual(config["base"]["confirmations"], 12)

    def test_validation(self):
        prices = {"input": 1, "cached_input": 0, "output": MAX_PRICE}
        Pricing.parse({"default": prices, "models": {"m" * 128: prices}})
        Pricing.parse({"default": prices,
                       "models": {f"m{i}": prices for i in range(MAX_MODELS)}})
        bad = [
            {"default": {**prices, "input": -1}},
            {"default": {**prices, "input": 1.0}},
            {"default": {**prices, "input": 0.5}},
            {"default": {**prices, "input": True}},
            {"default": {**prices, "input": "1"}},
            {"default": {**prices, "output": MAX_PRICE + 1}},
            {"default": {"input": 1, "output": 1}},
            {"default": {**prices, "discount": 1}},
            {"default": prices, "extra": {}},
            {"default": prices, "models": []},
            {"models": {}},
            {"default": prices, "models": {"": prices}},
            {"default": prices, "models": {" m": prices}},
            {"default": prices, "models": {"a\nb": prices}},
            {"default": prices, "models": {"m" * 129: prices}},
            {"default": prices, "models": {"m": {**prices, "input": -5}}},
            {"default": prices,
             "models": {f"m{i}": prices for i in range(MAX_MODELS + 1)}},
            [],
        ]
        for data in bad:
            with self.subTest(data=str(data)[:80]), self.assertRaises(PricingError):
                Pricing.parse(data)
        # A file may leave the default out; the admin API may not.
        self.assertEqual(Pricing.parse({}, require_default=False).default,
                         billing.pricing.DEFAULT_PRICES)

    def test_dumps_matches_the_example_format(self):
        path = ROOT / "tools/monitor/pricing.example.json"
        self.assertEqual(Pricing.load(path).dumps(), path.read_text())
        self.assertEqual(json.loads(Pricing().dumps()),
                         {"default": billing.pricing.DEFAULT_PRICES, "models": {}})


class PriceListTest(unittest.TestCase):
    TABLE = {"default": {"input": 300_000, "cached_input": 30_000, "output": 900_000},
             "models": {"m": {"input": 1, "cached_input": 2, "output": 3}}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "pricing.json"
        self.path.write_text('{"default": {"input": 1, "cached_input": 1, "output": 1}}')
        self.path.chmod(0o640)
        self.prices = PriceList(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_replaces_file_and_table(self):
        before = self.prices.current
        saved = self.prices.save(self.TABLE)
        self.assertIsNot(self.prices.current, before)
        self.assertEqual(before.default, {"input": 1, "cached_input": 1, "output": 1})
        self.assertEqual(saved.to_dict(), self.TABLE)
        self.assertEqual(Pricing.load(self.path).to_dict(), self.TABLE)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["pricing.json"])

    def test_invalid_or_failed_save_changes_nothing(self):
        text, current = self.path.read_text(), self.prices.current
        with self.assertRaises(PricingError):
            self.prices.save({"default": {"input": -1, "cached_input": 0, "output": 0}})
        with mock.patch("billing.pricing.os.replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            self.prices.save(self.TABLE)
        self.assertEqual(self.path.read_text(), text)
        self.assertIs(self.prices.current, current)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["pricing.json"])

    def test_readers_see_whole_tables_during_saves(self):
        tables = [self.TABLE, {"default": {"input": 2_000_000, "cached_input": 0,
                                           "output": 5_000_000}, "models": {}}]
        costs = {Pricing.parse(t).cost(None, 1000, 200, 100) for t in tables}
        stop = threading.Event()

        def writer():
            for i in range(40):
                self.prices.save(tables[i % 2])
            stop.set()
        thread = threading.Thread(target=writer)
        thread.start()
        seen = set()
        while not stop.is_set():
            seen.add(self.prices.current.cost(None, 1000, 200, 100))
        thread.join()
        self.assertLessEqual(seen - {1}, costs)  # 1: the table before the first save
        self.assertEqual(Pricing.load(self.path).to_dict(), tables[1])


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = bdb.Database(Path(self.tmp.name) / "billing.sqlite")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_ledger_is_append_only_and_idempotent(self):
        user = self.db.signup("a@example.com", "0123456789")
        with self.db.write() as conn:
            self.assertEqual(bdb.Database.post(conn, user, "deposit", 500, "solana:x"), (0, 500))
            self.assertIsNone(bdb.Database.post(conn, user, "deposit", 500, "solana:x"))
            self.assertEqual(bdb.Database.post(conn, user, "usage", -700, "r1@1"), (500, -200))
        self.assertEqual(self.db.user(user)["balance_micro"], -200)
        for statement in ("UPDATE ledger SET amount_micro=0", "DELETE FROM ledger"):
            with self.assertRaises(sqlite3.DatabaseError):
                with self.db.write() as conn:
                    conn.execute(statement)
        self.assertEqual(len(self.db.query("SELECT * FROM ledger")), 2)

    def test_passwords_and_sessions(self):
        user = self.db.signup("b@example.com", "correct horse")
        self.assertIsNone(self.db.signup("b@example.com", "something else"))
        self.assertNotIn("correct", self.db.user(user)["password"])
        self.assertEqual(self.db.authenticate("b@example.com", "correct horse"), user)
        self.assertIsNone(self.db.authenticate("b@example.com", "wrong horse!"))
        self.assertIsNone(self.db.authenticate("nobody@example.com", "correct horse"))
        session = self.db.create_session(user, 60)
        self.assertEqual(self.db.session_user(session), user)
        self.assertIsNone(self.db.session_user(session + "x"))
        self.db.delete_session(session)
        self.assertIsNone(self.db.session_user(session))

    def test_key_names(self):
        self.assertEqual(bdb.key_owner("u12-3"), 12)
        for name in ("owner", "monitor", "u12", "u-1", "x12-3", "u1-2-3", None):
            self.assertIsNone(bdb.key_owner(name))


class KeySyncFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "api_keys"
        self.store = gm.KeyStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_keeps_other_lines(self):
        self.path.write_text("# owner\n" + OWNER_LINE + "\nu1-1 sha256:" + "cd" * 32 + "\n")
        managed = lambda name: bdb.key_owner(name) is not None  # noqa: E731
        wanted = {"u2-1": {"digest": "ef" * 32, "models": [MODEL]}}
        self.assertTrue(self.store.sync_managed(wanted, managed))
        self.assertEqual(self.path.read_text(), "# owner\n" + OWNER_LINE + "\nu2-1 sha256:"
                         + "ef" * 32 + f" models={MODEL}\n")
        self.assertFalse(self.store.sync_managed(wanted, managed))
        self.assertTrue(self.store.sync_managed({}, managed))
        self.assertEqual(self.path.read_text(), "# owner\n" + OWNER_LINE + "\n")

    def test_never_empties_the_file(self):
        self.path.write_text("u1-1 sha256:" + "cd" * 32 + "\n")
        with self.assertRaises(gm.KeyFileError):
            self.store.sync_managed({}, lambda name: True)
        with self.assertRaises(gm.KeyFileError):
            self.store.sync_managed({"owner": {"digest": "ab" * 32, "models": []}},
                                    lambda name: name.startswith("u"))


class BillingHarness(unittest.TestCase):
    """A billing service with temp files, served with the monitor on one listener."""

    PUBLIC_URL = "https://gufo.test"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.keys_path = base / "keys" / "api_keys"
        self.keys_path.parent.mkdir()
        self.keys_path.write_text(OWNER_LINE + "\n")
        (base / "pricing.json").write_text(json.dumps({"default": {
            "input": 1_000_000, "cached_input": 100_000, "output": 3_000_000}}))
        (base / "billing.json").write_text(json.dumps({
            "db": "billing.sqlite", "pricing": "pricing.json", "signup_grant_micro": 1000,
            "max_keys_per_user": 2,
            "solana": {"receiver_owner": crypto.b58encode(b"\x07" * 32),
                       "receiver_token_account": crypto.b58encode(b"\x08" * 32)},
            "base": {"receiver": "0x" + "11" * 20},
        }))
        # GUFO_PUBLIC_URL as it may come from .env, with a trailing slash.
        config = billing.load_config(base / "billing.json", self.PUBLIC_URL + "/")
        self.chains = {"solana": FakeChain(), "base": FakeChain()}
        self.store = gm.KeyStore(self.keys_path)
        self.billing = billing.Billing(config, self.store, lambda: MODEL, self.chains)
        self.monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        self.monitor.billing = self.billing
        self.server = serve(gm.Server(("127.0.0.1", 0), self.monitor, gm.AdminAuth(ADMIN_TOKEN),
                                      False, self.store, billing=self.billing))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.billing.db.close()
        self.tmp.cleanup()

    # -- clients

    def call(self, port, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = raw
        return response.status, payload, response

    def portal_call(self, method, path, body=None, cookie=None, csrf=True, headers=None):
        headers = dict(headers or {})
        if csrf:
            headers["X-Gufo-Portal"] = "1"
        if cookie:
            headers["Cookie"] = "gufo_portal=" + cookie
        return self.call(self.server.server_address[1], method, path, body, headers)

    def admin_call(self, method, path, body=None, admin=True, headers=None):
        headers = dict(headers or {})
        if admin:
            headers["X-Admin-Token"] = ADMIN_TOKEN
        return self.call(self.server.server_address[1], method, path, body, headers)

    def account(self, email):
        status, payload, _ = self.admin_call("GET", "/api/admin/accounts")
        self.assertEqual(status, 200)
        return next(a for a in payload["accounts"] if a["email"] == email)

    def signup_and_login(self, email="friend@example.com", password="long enough pw"):
        status, payload, _ = self.portal_call("POST", "/api/portal/signup",
                                              {"email": email, "password": password})
        self.assertEqual((status, payload), (201, {"status": "pending_verification"}))
        status, payload, response = self.portal_call("POST", "/api/portal/login",
                                                     {"email": email, "password": password})
        self.assertEqual((status, payload), (200, {"ok": True}))
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        return cookie.split(";")[0].split("=", 1)[1]

    def verify(self, email):
        link = self.account(email)["verification_link"]
        self.assertTrue(link.startswith(self.PUBLIC_URL + "/api/portal/verify?token="), link)
        status, _, response = self.portal_call("GET", link[len(self.PUBLIC_URL):])
        self.assertEqual((status, response.getheader("Location")), (302, "/?verified=1"))

    def approved_user(self, email="friend@example.com"):
        cookie = self.signup_and_login(email)
        self.verify(email)
        user_id = self.account(email)["id"]
        status, payload, _ = self.admin_call("POST", f"/api/admin/accounts/{user_id}/approve")
        self.assertEqual(status, 200)
        return cookie, user_id, payload

    def me(self, cookie):
        status, payload, _ = self.portal_call("GET", "/api/portal/me", cookie=cookie)
        self.assertEqual(status, 200)
        return payload

    def link_solana(self, cookie):
        import nacl.signing
        signer = nacl.signing.SigningKey.generate()
        address = crypto.b58encode(bytes(signer.verify_key))
        status, payload, _ = self.portal_call("POST", "/api/portal/wallet/challenge",
                                              {"chain": "solana", "address": address}, cookie)
        self.assertEqual(status, 200)
        message = payload["message"]
        self.assertIn(address, message)
        self.assertIn("gufo.test", message)
        signature = crypto.b58encode(signer.sign(message.encode()).signature)
        status, payload, _ = self.portal_call("POST", "/api/portal/wallet/link", {
            "chain": "solana", "address": address, "signature": signature}, cookie)
        self.assertEqual((status, payload), (200, {"ok": True}))
        return address, signer


class AccountFlowTest(BillingHarness):
    def test_signup_verify_approve_key_meter_cutoff_and_restore(self):
        cookie = self.signup_and_login()
        me = self.me(cookie)
        self.assertEqual(me["status"], "pending_verification")
        self.assertEqual(me["balance_micro"], 0)
        self.assertEqual(me["pricing"], {"input": 1_000_000, "cached_input": 100_000,
                                         "output": 3_000_000})
        self.assertEqual(me["model"], MODEL)
        self.assertEqual(set(me["chains"]), {"solana", "base"})
        self.assertEqual(me["chains"]["base"]["chain_id"], 8453)

        # Approval needs a verified email; keys need approval.
        user_id = self.account("friend@example.com")["id"]
        status, payload, _ = self.admin_call("POST", f"/api/admin/accounts/{user_id}/approve")
        self.assertEqual((status, payload["error"]), (409, "not_verified"))
        status, payload, _ = self.portal_call("POST", "/api/portal/keys", {"label": "x"}, cookie)
        self.assertEqual((status, payload["error"]), (403, "not_approved"))

        self.verify("friend@example.com")
        account = self.account("friend@example.com")
        self.assertEqual((account["status"], account["email_verified"],
                          account["verification_link"]), ("pending_approval", True, None))
        status, _, response = self.portal_call("GET", "/api/portal/verify?token=nope")
        self.assertEqual(response.getheader("Location"), "/?verified=0")

        status, payload, _ = self.admin_call("POST", f"/api/admin/accounts/{user_id}/approve")
        self.assertEqual((status, payload["ok"], payload["grant_micro"]), (200, True, 1000))
        status, payload, _ = self.admin_call("POST", f"/api/admin/accounts/{user_id}/approve")
        self.assertEqual(payload["grant_micro"], 0)  # granted once only
        self.assertEqual(self.me(cookie)["balance_micro"], 1000)

        status, payload, _ = self.portal_call("POST", "/api/portal/keys", {"label": "laptop"},
                                              cookie)
        self.assertEqual(status, 201)
        name, token = payload["name"], payload["token"]
        self.assertEqual(name, f"u{user_id}-1")
        self.assertTrue(token.startswith("gufo-") and len(token) == 45)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertEqual(self.keys_path.read_text(), OWNER_LINE + "\n"
                         + f"{name} sha256:{digest} models={MODEL}\n")
        self.assertNotIn(token, self.keys_path.read_text())
        self.assertEqual(self.me(cookie)["keys"][0]["enabled"], True)

        # A completed request from the log debits the owner and cuts the key off.
        tail = gm.LogTail("file:/dev/null", self.monitor, show_clients=False, log_utc=True)
        tail.feed(log_line("owner", "r0", 0))  # not a portal key: never billed
        tail.feed(log_line(name, "r1", 1))
        me = self.me(cookie)
        self.assertEqual(me["balance_micro"], 1000 - LINE_COST)
        self.assertEqual(me["keys"][0]["enabled"], False)
        self.assertIsNotNone(me["keys"][0]["last_used"])
        self.assertEqual(self.keys_path.read_text(), OWNER_LINE + "\n")
        status, payload, _ = self.portal_call("GET", "/api/portal/ledger", cookie=cookie)
        self.assertEqual([(e["kind"], e["amount_micro"], e["balance_after_micro"])
                          for e in payload["entries"]],
                         [("usage", -LINE_COST, 1000 - LINE_COST), ("signup_grant", 1000, 1000)])

        # An admin credit restores the key.
        status, payload, _ = self.admin_call("POST", f"/api/admin/accounts/{user_id}/credit",
                                             {"amount_micro": 5000, "note": "thanks"})
        self.assertEqual((status, payload["balance_micro"]), (200, 1000 - LINE_COST + 5000))
        self.assertIn(f"{name} sha256:{digest}", self.keys_path.read_text())

        # Suspension removes the keys; unsuspending brings them back.
        self.admin_call("POST", f"/api/admin/accounts/{user_id}/suspend")
        self.assertEqual(self.keys_path.read_text(), OWNER_LINE + "\n")
        self.admin_call("POST", f"/api/admin/accounts/{user_id}/unsuspend")
        self.assertIn(name, self.keys_path.read_text())

        account = self.account("friend@example.com")
        self.assertEqual((account["spent_micro"], account["keys"], account["balance_micro"]),
                         (LINE_COST, 1, 1000 - LINE_COST + 5000))
        status, payload, _ = self.admin_call("GET", "/api/admin/accounts")
        self.assertEqual(payload["totals"]["users"], 1)

        # Key limit, and deleting a key takes it out of the file.
        self.assertEqual(self.portal_call("POST", "/api/portal/keys", {}, cookie)[0], 201)
        status, payload, _ = self.portal_call("POST", "/api/portal/keys", {}, cookie)
        self.assertEqual((status, payload["error"]), (409, "key_limit"))
        status, payload, _ = self.portal_call("DELETE", f"/api/portal/keys/{name}", None, cookie)
        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertNotIn(name + " ", self.keys_path.read_text())
        self.assertEqual(self.portal_call("DELETE", f"/api/portal/keys/{name}", None,
                                          cookie)[0], 404)

    def test_replayed_log_does_not_double_debit(self):
        cookie, user_id, _ = self.approved_user()
        self.admin_call("POST", f"/api/admin/accounts/{user_id}/credit",
                        {"amount_micro": 100_000})
        name = self.portal_call("POST", "/api/portal/keys", {}, cookie)[1]["name"]
        first = gm.LogTail("file:/dev/null", self.monitor, show_clients=False, log_utc=True)
        for second, request in ((1, "r1"), (2, "r2")):
            first.feed(log_line(name, request, second))
        balance = self.me(cookie)["balance_micro"]
        self.assertEqual(balance, 101_000 - 2 * LINE_COST)
        # A restarted monitor re-reads the same window of the log.
        replay = gm.LogTail("file:/dev/null", self.monitor, show_clients=False, log_utc=True)
        for second, request in ((1, "r1"), (2, "r2")):
            replay.feed(log_line(name, request, second))
        self.assertEqual(self.me(cookie)["balance_micro"], balance)
        # Gufo restarts its request ids: r1 again, later, is a new request.
        replay.feed(log_line(name, "r1", 30))
        self.assertEqual(self.me(cookie)["balance_micro"], balance - LINE_COST)
        # A line older than the newest billed one is ignored.
        replay.feed(log_line(name, "r9", 5))
        self.assertEqual(self.me(cookie)["balance_micro"], balance - LINE_COST)
        usage = self.billing.db.query("SELECT COUNT(*) AS n FROM ledger WHERE kind='usage'")
        self.assertEqual(usage[0]["n"], 3)

    def test_unknown_keys_are_not_billed(self):
        cookie, user_id, _ = self.approved_user()
        # A well-formed name with no key row, or another user's number, bills nobody.
        self.assertIsNone(self.billing.meter.on_request(gm.request_record(
            gm.parse_log_line(log_line(f"u{user_id}-7")), False)))
        self.assertEqual(self.me(cookie)["balance_micro"], 1000)


class UsageAdminTest(BillingHarness):
    NOW = 1_793_102_400  # 2026-10-27 08:00:00 UTC

    def record(self, key="owner", request="r1", t=None):
        return {"key": key, "request": request, "t": self.NOW if t is None else t,
                "model": MODEL, "prompt_tokens": 1000, "cached_tokens": 200,
                "generated_tokens": 100}

    def test_internal_is_idempotent_and_never_charged(self):
        cookie, _, _ = self.approved_user()
        keys = self.keys_path.read_text()
        ledger = [dict(row) for row in self.billing.db.query("SELECT * FROM ledger")]
        balance = self.me(cookie)["balance_micro"]
        for key in ("owner", "hand-made"):
            record = self.record(key, key)
            self.assertIsNone(self.billing.meter.on_request(record))
            self.billing.meter.on_request(record)
        for key in (None, ""):
            self.billing.meter.on_request(self.record(key, "no-key"))
        rows = self.billing.db.query("SELECT * FROM usage_log ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["key_name"] for r in rows], ["owner", "hand-made"])
        self.assertTrue(all(r["kind"] == "internal" and r["user_id"] is None for r in rows))
        self.assertEqual(rows[0]["cost_micro"], LINE_COST)
        self.assertEqual(rows[0]["model"], MODEL)
        self.assertEqual(self.keys_path.read_text(), keys)
        self.assertEqual(self.me(cookie)["balance_micro"], balance)
        self.assertEqual([dict(row) for row in self.billing.db.query("SELECT * FROM ledger")],
                         ledger)
        self.assertIsNone(self.billing.db.meta("last_metered_t"))
        self.billing.save_pricing({"default": {"input": 0, "cached_input": 0, "output": 0}})
        self.billing.meter.on_request(self.record(request="later", t=self.NOW + 1))
        self.assertEqual([r["cost_micro"] for r in self.billing.db.query(
            "SELECT cost_micro FROM usage_log ORDER BY id")], [LINE_COST, LINE_COST, 0])
        # Existing databases gain the table on open, with no schema migration.
        self.billing.db.conn.executescript(bdb.SCHEMA)

    def test_user_tokens_and_ledger_are_written_together(self):
        cookie, user_id, _ = self.approved_user()
        name = self.portal_call("POST", "/api/portal/keys", {}, cookie)[1]["name"]
        record = self.record(name)
        self.assertEqual(self.billing.meter.on_request(record), LINE_COST)
        self.assertIsNone(self.billing.meter.on_request(record))
        row = self.billing.db.one("SELECT * FROM usage_log")
        self.assertEqual((row["kind"], row["user_id"], row["prompt_tokens"],
                          row["cached_tokens"], row["generated_tokens"], row["cost_micro"]),
                         ("user", user_id, 1000, 200, 100, LINE_COST))
        with self.billing.db.write() as conn:
            conn.execute("DELETE FROM usage_log")  # simulate a debit from before this feature
        self.billing.meter.on_request(record)
        self.assertEqual(self.billing.db.query("SELECT * FROM usage_log"), [])
        report = self.billing.admin_usage(30, now=self.NOW)
        self.assertEqual(report["users"]["totals"],
                         {"requests": 1, "prompt": 0, "cached": 0, "generated": 0,
                          "cost_micro": LINE_COST, "tokens_unknown_requests": 1})

    def test_revenue_splits_users_and_internal_inside_the_window(self):
        cookie, user_id, _ = self.approved_user()
        name = self.portal_call("POST", "/api/portal/keys", {}, cookie)[1]["name"]
        # The meter takes user requests in time order: oldest first.
        self.billing.meter.on_request(self.record(name, "old", self.NOW - 30 * 3600))
        self.billing.meter.on_request(self.record(name, "user", self.NOW - 3600))
        self.billing.meter.on_request(self.record("hand-made", "internal", self.NOW - 60))
        self.billing.meter.on_request(self.record("hand-made", "older", self.NOW - 40 * 3600))
        self.assertEqual(self.billing.revenue_micro(24, now=self.NOW),
                         {"users": LINE_COST, "internal": LINE_COST, "total": 2 * LINE_COST})
        self.assertEqual(self.billing.revenue_micro(168, now=self.NOW),
                         {"users": 2 * LINE_COST, "internal": 2 * LINE_COST,
                          "total": 4 * LINE_COST})

    def test_user_usage_rolls_back_if_debit_fails(self):
        cookie, _, _ = self.approved_user()
        name = self.portal_call("POST", "/api/portal/keys", {}, cookie)[1]["name"]
        with mock.patch.object(bdb.Database, "post", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                self.billing.meter.on_request(self.record(name))
        self.assertEqual(self.billing.db.query("SELECT * FROM usage_log"), [])

    def test_api_days_auth_and_utc_buckets(self):
        # These timestamps straddle a UTC midnight, regardless of the host timezone.
        midnight = (self.NOW // 86400) * 86400
        self.billing.meter.on_request(self.record(t=midnight - 1))
        self.billing.meter.on_request(self.record("hand-made", "r2", midnight))
        self.billing.meter.on_request(self.record("old", "r0", midnight - 31 * 86400))
        cookie, user_id, _ = self.approved_user()
        name = self.portal_call("POST", "/api/portal/keys", {}, cookie)[1]["name"]
        self.billing.meter.on_request(self.record(name, "user", midnight + 1))
        with self.billing.db.write() as conn:
            bdb.Database.post(conn, user_id, "usage", -17, "legacy", t=midnight - 1)
        with mock.patch.object(billing.time, "time", return_value=self.NOW):
            status, data, _ = self.admin_call("GET", "/api/admin/usage")
            self.assertEqual(status, 200)
            self.assertEqual(data["days"], 30)
            self.assertEqual(data["internal"]["totals"]["requests"], 2)
            self.assertEqual([r["day"] for r in data["internal"]["by_day"]],
                             ["2026-10-26", "2026-10-27"])
            self.assertEqual(data["users"]["totals"],
                             {"requests": 2, "prompt": 1000, "cached": 200, "generated": 100,
                              "cost_micro": LINE_COST + 17, "tokens_unknown_requests": 1})
            self.assertEqual([r["key"] for r in data["internal"]["by_key"]],
                             ["hand-made", "owner"])
            status, data, _ = self.admin_call("GET", "/api/admin/usage?days=1")
            self.assertEqual(data["internal"]["totals"]["requests"], 1)
            for days in ("", "0", "-1", "366", "1.5", "abc", "1&days=2"):
                status, payload, _ = self.admin_call("GET", "/api/admin/usage?days=" + days)
                self.assertEqual((status, payload["error"]), (400, "bad_days"))
            self.assertEqual(self.admin_call("GET", "/api/admin/usage?days=365")[0], 200)
        for headers in ({}, {"Cookie": "gufo_portal=" + cookie}):
            self.assertEqual(self.admin_call("GET", "/api/admin/usage", admin=False,
                                             headers=headers)[0], 401)


class PricingAdminTest(BillingHarness):
    TABLE = {"default": {"input": 2_000_000, "cached_input": 200_000, "output": 6_000_000},
             "models": {MODEL: {"input": 300_000, "cached_input": 30_000, "output": 900_000},
                        "other": {"input": 0, "cached_input": 0, "output": 0}}}

    def pricing_path(self):
        return Path(self.tmp.name) / "pricing.json"

    def test_round_trip(self):
        status, payload, _ = self.admin_call("GET", "/api/admin/pricing")
        self.assertEqual((status, payload), (200, {
            "default": {"input": 1_000_000, "cached_input": 100_000, "output": 3_000_000},
            "models": {}, "served_models": []}))
        self.monitor.models = [{"id": MODEL, "context_length": 1024}]
        status, payload, _ = self.admin_call("PUT", "/api/admin/pricing", self.TABLE)
        self.assertEqual((status, payload), (200, {**self.TABLE, "served_models": [MODEL]}))
        status, payload, _ = self.admin_call("GET", "/api/admin/pricing")
        self.assertEqual(payload, {**self.TABLE, "served_models": [MODEL]})
        self.assertEqual(json.loads(self.pricing_path().read_text()), self.TABLE)
        # Removing a model is saving the table without it.
        table = {"default": self.TABLE["default"], "models": {}}
        self.assertEqual(self.admin_call("PUT", "/api/admin/pricing", table)[0], 200)
        self.assertEqual(json.loads(self.pricing_path().read_text()), table)

    def test_invalid_tables_are_refused(self):
        text = self.pricing_path().read_text()
        for body in ({"models": {}},
                     {"default": {"input": -1, "cached_input": 0, "output": 0}},
                     {"default": {"input": 0.3, "cached_input": 0, "output": 0}},
                     {**self.TABLE, "models": {"": self.TABLE["default"]}}):
            status, payload, _ = self.admin_call("PUT", "/api/admin/pricing", body)
            self.assertEqual((status, payload["error"]), (400, "bad_pricing"))
            self.assertTrue(payload["message"])
        status, payload, _ = self.admin_call("PUT", "/api/admin/pricing", None,
                                             headers={"Content-Type": "application/json"})
        self.assertEqual((status, payload["error"]), (400, "bad_request"))
        status, payload, _ = self.admin_call("POST", "/api/admin/pricing", self.TABLE)
        self.assertEqual(status, 405)
        self.assertEqual(self.pricing_path().read_text(), text)

    def test_only_the_admin(self):
        text = self.pricing_path().read_text()
        cookie, _, _ = self.approved_user()
        user = {"Cookie": "gufo_portal=" + cookie, "X-Gufo-Portal": "1"}
        for headers in ({}, user, {"X-Admin-Token": "wrong"}):
            status, _, _ = self.admin_call("GET", "/api/admin/pricing", admin=False,
                                           headers=headers)
            self.assertEqual(status, 401)
            status, _, _ = self.admin_call("PUT", "/api/admin/pricing", self.TABLE, admin=False,
                                           headers=headers)
            self.assertEqual(status, 401)
        self.assertEqual(self.pricing_path().read_text(), text)

    def test_new_prices_bill_later_requests_only(self):
        cookie, user_id, _ = self.approved_user()
        self.admin_call("POST", f"/api/admin/accounts/{user_id}/credit",
                        {"amount_micro": 100_000})
        name = self.portal_call("POST", "/api/portal/keys", {}, cookie)[1]["name"]
        tail = gm.LogTail("file:/dev/null", self.monitor, show_clients=False, log_utc=True)
        tail.feed(log_line(name, "r1", 1))
        self.assertEqual(self.admin_call("PUT", "/api/admin/pricing", self.TABLE)[0], 200)
        tail.feed(log_line(name, "r2", 2))  # no model in the line: the default prices
        usage = [row["amount_micro"] for row in self.billing.db.query(
            "SELECT amount_micro FROM ledger WHERE kind='usage' ORDER BY id")]
        self.assertEqual(usage, [-LINE_COST, -2 * LINE_COST])
        self.assertEqual(self.me(cookie)["balance_micro"], 101_000 - 3 * LINE_COST)
        # The portal shows the served model's entry, else the default.
        self.assertEqual(self.me(cookie)["pricing"], self.TABLE["models"][MODEL])
        self.admin_call("PUT", "/api/admin/pricing",
                        {"default": self.TABLE["default"], "models": {}})
        self.assertEqual(self.me(cookie)["pricing"], self.TABLE["default"])


class DepositTest(BillingHarness):
    def test_solana_deposit_flow(self):
        cookie, user_id, _ = self.approved_user()
        address, _ = self.link_solana(cookie)
        self.assertEqual(self.me(cookie)["wallets"], [{"chain": "solana", "address": address}])
        fake = self.chains["solana"]
        tx = crypto.b58encode(b"\x05" * 64)
        stranger = crypto.b58encode(b"\x09" * 32)

        # Not on chain yet: stored pending, re-checked in the background.
        status, payload, _ = self.portal_call("POST", "/api/portal/deposit",
                                              {"chain": "solana", "tx": tx}, cookie)
        self.assertEqual((status, payload["status"], payload["amount_micro"]),
                         (200, "pending", 0))
        fake.answers[tx] = chain.DepositCheck("confirmed", address, 2_500_000, "")
        self.billing.recheck_pending()
        self.assertEqual(self.me(cookie)["balance_micro"], 1000 + 2_500_000)
        # Asking again reports the same credit, once.
        status, payload, _ = self.portal_call("POST", "/api/portal/deposit",
                                              {"chain": "solana", "tx": tx}, cookie)
        self.assertEqual(payload, {"status": "credited", "amount_micro": 2_500_000, "reason": ""})
        self.assertEqual(self.me(cookie)["balance_micro"], 1000 + 2_500_000)
        self.assertEqual(self.account("friend@example.com")["deposited_micro"], 2_500_000)

        # A transfer from a wallet this account has not linked is refused.
        other_tx = crypto.b58encode(b"\x06" * 64)
        fake.answers[other_tx] = chain.DepositCheck("confirmed", stranger, 1_000_000, "")
        payload = self.portal_call("POST", "/api/portal/deposit",
                                   {"chain": "solana", "tx": other_tx}, cookie)[1]
        self.assertEqual((payload["status"], payload["reason"]), ("rejected", "sender_not_linked"))

        # Another account cannot claim a credited transaction.
        cookie2, _, _ = self.approved_user("second@example.com")
        payload = self.portal_call("POST", "/api/portal/deposit",
                                   {"chain": "solana", "tx": tx}, cookie2)[1]
        self.assertEqual((payload["status"], payload["reason"]), ("rejected", "already_claimed"))
        self.assertEqual(self.me(cookie2)["balance_micro"], 1000)

        # A wrong signature links nothing; garbage transaction ids are refused.
        status, payload, _ = self.portal_call("POST", "/api/portal/wallet/challenge",
                                              {"chain": "solana", "address": stranger}, cookie)
        self.assertEqual(status, 200)
        status, payload, _ = self.portal_call("POST", "/api/portal/wallet/link", {
            "chain": "solana", "address": stranger, "signature": crypto.b58encode(b"\0" * 64)},
            cookie)
        self.assertEqual((status, payload["error"]), (400, "bad_signature"))

        status, payload, _ = self.portal_call("POST", "/api/portal/deposit",
                                              {"chain": "solana", "tx": "not-a-sig"}, cookie)
        self.assertEqual((status, payload["error"]), (400, "bad_tx"))

    def test_wallet_signature_is_single_use_and_taken_wallets_refused(self):
        cookie, _, _ = self.approved_user()
        address, signer = self.link_solana(cookie)
        cookie2, _, _ = self.approved_user("second@example.com")
        message = self.portal_call("POST", "/api/portal/wallet/challenge",
                                   {"chain": "solana", "address": address}, cookie2)[1]["message"]
        signature = crypto.b58encode(signer.sign(message.encode()).signature)
        body = {"chain": "solana", "address": address, "signature": signature}
        status, payload, _ = self.portal_call("POST", "/api/portal/wallet/link", body, cookie2)
        self.assertEqual((status, payload["error"]), (409, "wallet_taken"))
        status, payload, _ = self.portal_call("POST", "/api/portal/wallet/link", body, cookie2)
        self.assertEqual((status, payload["error"]), (400, "no_challenge"))

    def test_base_link_and_deposit(self):
        cookie, _, _ = self.approved_user()
        address = "0x" + "Ab" * 20
        message = self.portal_call("POST", "/api/portal/wallet/challenge",
                                   {"chain": "base", "address": address}, cookie)[1]["message"]
        self.assertIn(address.lower(), message)
        original = crypto.eth_recover_personal
        crypto.eth_recover_personal = lambda m, s: address.lower() if m == message else None
        try:
            status, payload, _ = self.portal_call("POST", "/api/portal/wallet/link", {
                "chain": "base", "address": address, "signature": "0x" + "00" * 65}, cookie)
        finally:
            crypto.eth_recover_personal = original
        self.assertEqual((status, payload), (200, {"ok": True}))
        tx = "0x" + "CD" * 32
        self.chains["base"].answers[tx.lower()] = chain.DepositCheck(
            "confirmed", "0x" + "AB" * 20, 1_000_000, "")
        payload = self.portal_call("POST", "/api/portal/deposit", {"chain": "base", "tx": tx},
                                   cookie)[1]
        self.assertEqual(payload["status"], "credited")
        self.assertEqual(self.me(cookie)["balance_micro"], 1_001_000)

    def test_solana_prepare(self):
        cookie, _, _ = self.approved_user()
        address, _ = self.link_solana(cookie)
        source = crypto.b58encode(b"\x0a" * 32)
        status, payload, _ = self.portal_call("POST", "/api/portal/solana/prepare",
                                              {"owner": address, "amount_micro": 5_000_000},
                                              cookie)
        self.assertEqual((status, payload["error"]), (400, "no_usdc_account"))
        self.chains["solana"].accounts[address] = [source]
        status, payload, _ = self.portal_call("POST", "/api/portal/solana/prepare",
                                              {"owner": address, "amount_micro": 5_000_000},
                                              cookie)
        self.assertEqual((status, payload["source_token_account"]), (200, source))
        wire = base64.b64decode(payload["transaction"])
        self.assertEqual(wire[:65], b"\x01" + b"\0" * 64)
        self.assertIn((5_000_000).to_bytes(8, "little"), wire)
        for amount in (999_999, 1_000_000_001, 1.5):
            status, payload, _ = self.portal_call("POST", "/api/portal/solana/prepare",
                                                  {"owner": address, "amount_micro": amount},
                                                  cookie)
            self.assertEqual((status, payload["error"]), (400, "bad_amount"))
        stranger = crypto.b58encode(b"\x09" * 32)
        status, payload, _ = self.portal_call("POST", "/api/portal/solana/prepare",
                                              {"owner": stranger, "amount_micro": 5_000_000},
                                              cookie)
        self.assertEqual((status, payload["error"]), (403, "wallet_not_linked"))

    def test_unapproved_accounts_cannot_deposit(self):
        cookie = self.signup_and_login()
        status, payload, _ = self.portal_call("POST", "/api/portal/deposit",
                                              {"chain": "solana", "tx": "1" * 64}, cookie)
        self.assertEqual((status, payload["error"]), (403, "not_approved"))

    def test_pending_deposits_expire(self):
        cookie, user_id, _ = self.approved_user()
        tx = crypto.b58encode(b"\x04" * 64)
        self.portal_call("POST", "/api/portal/deposit", {"chain": "solana", "tx": tx}, cookie)
        with self.billing.db.write() as conn:
            conn.execute("UPDATE deposits SET created=created-90000")
        self.billing.recheck_pending()
        row = self.billing.db.one("SELECT status, reason FROM deposits")
        self.assertEqual((row["status"], row["reason"]), ("rejected", "expired"))


class SecurityTest(BillingHarness):
    def test_portal_requires_csrf_header_and_json(self):
        status, payload, _ = self.portal_call("POST", "/api/portal/signup",
                                              {"email": "a@example.com", "password": "x" * 12},
                                              csrf=False)
        self.assertEqual((status, payload["error"]), (403, "csrf"))
        status, payload, _ = self.portal_call("POST", "/api/portal/signup", None, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": "5"})
        self.assertEqual(status, 400)
        status, payload, _ = self.portal_call("DELETE", "/api/portal/keys/u1-1", csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.billing.db.query("SELECT COUNT(*) AS n FROM users")[0]["n"], 0)

    def test_portal_auth_and_scope(self):
        status, payload, response = self.portal_call("GET", "/api/portal/me")
        self.assertEqual((status, payload["error"]), (401, "unauthorized"))
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        # One listener: the public side reaches no monitor data without a sign-in, and a
        # portal session is not one.
        cookie = self.signup_and_login()
        for path in ("/api/state", "/api/admin/accounts", "/api/keys", "/api/stream"):
            for session in (None, cookie):
                self.assertEqual(self.portal_call("GET", path, cookie=session)[0], 401, path)
        status, _, response = self.portal_call("GET", "/monitor", cookie=cookie)
        self.assertEqual((status, response.getheader("Location")), (303, "/"))
        for path in ("/api/admin/accounts/1/verify", "/api/admin/accounts/1/approve",
                     "/api/keys"):
            self.assertEqual(self.portal_call("POST", path, {}, cookie)[0], 401, path)
        self.assertEqual(self.portal_call("GET", "/static/../billing.json")[0], 404)
        status, payload, _ = self.portal_call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'src="/static/portal.js"', payload)
        self.assertEqual(self.portal_call("POST", "/api/portal/keys", {})[0], 401)
        self.assertEqual(self.billing.db.query("SELECT email_verified FROM users")[0][0], 0)
        status, payload, _ = self.portal_call("POST", "/api/portal/signup",
                                              {"email": "bad", "password": "x" * 12})
        self.assertEqual(payload["error"], "invalid_email")
        status, payload, _ = self.portal_call("POST", "/api/portal/signup",
                                              {"email": "a@example.com", "password": "short"})
        self.assertEqual(payload["error"], "weak_password")
        status, payload, _ = self.portal_call("POST", "/api/portal/login",
                                              {"email": "a@example.com", "password": "x" * 12})
        self.assertEqual((status, payload["error"]), (401, "bad_credentials"))

    def test_logout_ends_the_session(self):
        cookie = self.signup_and_login()
        status, _, response = self.portal_call("POST", "/api/portal/logout", {}, cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
        self.assertEqual(self.portal_call("GET", "/api/portal/me", cookie=cookie)[0], 401)

    def test_login_rate_limit(self):
        codes = [self.portal_call("POST", "/api/portal/login",
                                  {"email": "a@example.com", "password": "x" * 12})[0]
                 for _ in range(11)]
        self.assertEqual(codes, [401] * 10 + [429])

    def test_rate_limit_keys_on_cf_connecting_ip_from_a_local_tunnel(self):
        body = {"email": "a@example.com", "password": "x" * 12}
        tunnel = {"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.1"}
        codes = [self.portal_call("POST", "/api/portal/login", body, headers=tunnel)[0]
                 for _ in range(11)]
        self.assertEqual(codes, [401] * 10 + [429])
        # Another visitor through the same tunnel has their own budget.
        other = {"CF-Connecting-IP": "203.0.113.8", "X-Forwarded-For": "198.51.100.1"}
        self.assertEqual(self.portal_call("POST", "/api/portal/login", body, headers=other)[0], 401)

    def test_cookie_is_secure_behind_an_https_tunnel(self):
        self.portal_call("POST", "/api/portal/signup",
                         {"email": "a@example.com", "password": "long enough pw"})
        status, _, response = self.portal_call(
            "POST", "/api/portal/login", {"email": "a@example.com", "password": "long enough pw"},
            headers={"X-Forwarded-Proto": "https", "CF-Connecting-IP": "203.0.113.7"})
        self.assertEqual(status, 200)
        self.assertIn("; Secure", response.getheader("Set-Cookie"))

    def test_signup_does_not_reveal_existing_emails(self):
        body = {"email": "Friend@Example.com", "password": "long enough pw"}
        first = self.portal_call("POST", "/api/portal/signup", body)[:2]
        second = self.portal_call("POST", "/api/portal/signup", dict(body, password="other pw 123"))
        self.assertEqual(first, second[:2])
        self.assertEqual(self.billing.db.query("SELECT COUNT(*) AS n FROM users")[0]["n"], 1)

    MONITOR_ROUTES = ("/api/state", "/api/stream", "/api/keys", "/api/admin/accounts")

    def status_of(self, path, headers):
        """Status and Location of GET PATH, without reading the body (a stream never ends)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                                timeout=10)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        connection.close()
        return response.status, response.getheader("Location")

    def test_the_monitor_is_the_admins_alone(self):
        # A browser on the monitor's own host asking for a loopback name: what
        # --trust-localhost used to let in.
        local = {"Host": f"127.0.0.1:{self.server.server_address[1]}"}
        user, _, _ = self.approved_user()
        self.assertEqual(self.me(user)["status"], "approved")
        refused = {
            "no credentials": {},
            "approved portal user": {"Cookie": "gufo_portal=" + user},
            "forged monitor cookie": {"Cookie": "gufo_monitor=" + secrets.token_hex(32)},
            "random bearer": {"Authorization": "Bearer " + secrets.token_urlsafe(32)},
            "forged admin cookie": {
                "Cookie": f"{gm.ADMIN_COOKIE_NAME}=9999999999.{secrets.token_hex(32)}"},
        }
        for name, headers in refused.items():
            headers = {**local, **headers}
            for path in self.MONITOR_ROUTES:
                self.assertEqual(self.status_of(path, headers), (401, None), (name, path))
            self.assertEqual(self.status_of("/monitor", headers), (303, "/"), name)
            for path in ("/api/admin/accounts/1/approve", "/api/keys"):
                self.assertEqual(self.admin_call("POST", path, {}, False, headers)[0], 401,
                                 (name, path))
            self.assertEqual(self.call(self.server.server_address[1], "GET",
                                       "/api/admin/session", headers=headers)[1],
                             {"admin": False}, name)
        status, _, response = self.call(self.server.server_address[1], "POST",
                                        "/api/admin/login", {"token": ADMIN_TOKEN}, local)
        self.assertEqual(status, 204)
        accepted = {
            "X-Admin-Token": {"X-Admin-Token": ADMIN_TOKEN},
            "Bearer": {"Authorization": "Bearer " + ADMIN_TOKEN},
            "admin cookie": {"Cookie": response.getheader("Set-Cookie").split(";")[0]},
        }
        for name, headers in accepted.items():
            headers = {**local, **headers}
            for path in (*self.MONITOR_ROUTES, "/monitor"):
                self.assertEqual(self.status_of(path, headers), (200, None), (name, path))
            self.assertEqual(self.call(self.server.server_address[1], "GET",
                                       "/api/admin/session", headers=headers)[1],
                             {"admin": True}, name)

    def test_admin_api_refuses_without_the_admin_token(self):
        self.signup_and_login()
        for headers in ({}, {"X-Admin-Token": "wrong"}, {"Authorization": "Bearer wrong"}):
            for method, path, body in (("GET", "/api/admin/accounts", None),
                                       ("POST", "/api/admin/accounts/1/approve", None),
                                       ("POST", "/api/admin/accounts/1/verify", None),
                                       ("POST", "/api/admin/accounts/1/credit",
                                        {"amount_micro": 5})):
                status, payload, _ = self.admin_call(method, path, body, False, headers)
                self.assertEqual((status, payload["error"]), (401, "unauthorized"),
                                 (method, path, headers))
        self.assertEqual(self.billing.db.query("SELECT COUNT(*) AS n FROM ledger")[0]["n"], 0)
        self.assertEqual(self.account("friend@example.com")["status"], "pending_verification")
        status, payload, _ = self.admin_call("POST", "/api/admin/accounts/1/credit",
                                             {"amount_micro": 1.5})
        self.assertEqual((status, payload["error"]), (400, "bad_amount"))
        status, payload, _ = self.admin_call("POST", "/api/admin/accounts/99/approve")
        self.assertEqual(status, 404)
        status, payload, _ = self.admin_call("POST", "/api/admin/accounts/1/explode")
        self.assertEqual(status, 404)

    def test_admin_and_portal_sign_ins_have_separate_budgets(self):
        self.server.limiter.limit = 2
        body = {"email": "a@example.com", "password": "x" * 12}
        codes = [self.portal_call("POST", "/api/portal/login", body)[0] for _ in range(3)]
        self.assertEqual(codes, [401, 401, 429])
        status, _, _ = self.call(self.server.server_address[1], "POST", "/api/admin/login",
                                 {"token": ADMIN_TOKEN})
        self.assertEqual(status, 204)

    def test_monitor_without_billing_has_no_account_routes(self):
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        plain = serve(gm.Server(("127.0.0.1", 0), monitor, gm.AdminAuth(ADMIN_TOKEN), False,
                                self.store))
        try:
            status, _, _ = self.call(plain.server_address[1], "GET", "/api/admin/accounts",
                                     headers={"X-Admin-Token": ADMIN_TOKEN})
            self.assertEqual(status, 404)
        finally:
            plain.shutdown()
            plain.server_close()


class AdminVerifyTest(BillingHarness):
    def test_one_click_verify_then_approve(self):
        cookie = self.signup_and_login()
        account = self.account("friend@example.com")
        link = account["verification_link"]
        self.assertTrue(link.startswith("https://gufo.test/api/portal/verify?token="), link)
        status, payload, _ = self.admin_call("POST", f"/api/admin/accounts/{account['id']}/verify")
        self.assertEqual((status, payload["ok"]), (200, True))
        account = self.account("friend@example.com")
        self.assertEqual((account["status"], account["email_verified"],
                          account["verification_link"]), ("pending_approval", True, None))
        self.assertEqual(self.me(cookie)["status"], "pending_approval")
        # Exactly like the link: done once, and the link is spent.
        status, payload, _ = self.admin_call("POST", f"/api/admin/accounts/{account['id']}/verify")
        self.assertEqual((status, payload["error"]), (409, "already_verified"))
        status, _, response = self.portal_call("GET", link[len("https://gufo.test"):])
        self.assertEqual(response.getheader("Location"), "/?verified=0")
        status, payload, _ = self.admin_call("POST", f"/api/admin/accounts/{account['id']}/approve")
        self.assertEqual((status, payload["grant_micro"]), (200, 1000))
        self.assertEqual(self.me(cookie)["status"], "approved")
        status, _, _ = self.admin_call("POST", "/api/admin/accounts/99/verify")
        self.assertEqual(status, 404)

    def test_admin_sign_in_from_the_portal_page(self):
        self.signup_and_login()
        status, _, response = self.call(self.server.server_address[1], "POST",
                                        "/api/admin/login", {"token": ADMIN_TOKEN},
                                        {"X-Forwarded-Proto": "https",
                                         "CF-Connecting-IP": "203.0.113.7"})
        self.assertEqual(status, 204)
        cookies = [c.split(";")[0] for c in response.headers.get_all("Set-Cookie")]
        self.assertEqual([c.split("=")[0] for c in cookies], [gm.ADMIN_COOKIE_NAME])
        headers = {"Cookie": cookies[0]}
        port = self.server.server_address[1]
        status, payload, _ = self.call(port, "GET", "/api/admin/session", headers=headers)
        self.assertEqual(payload, {"admin": True})
        status, payload, _ = self.call(port, "GET", "/api/admin/accounts", headers=headers)
        self.assertEqual((status, payload["accounts"][0]["email"]), (200, "friend@example.com"))
        status, page, _ = self.call(port, "GET", "/monitor", headers=headers)
        self.assertEqual(status, 200)
        self.assertIn(b"accounts-section", page)
        # An admin-only session has no portal account; /api/portal/me just says so.
        status, payload, _ = self.portal_call("GET", "/api/portal/me", headers=headers)
        self.assertEqual((status, payload["error"]), (401, "unauthorized"))


class ConfigTest(unittest.TestCase):
    def test_relative_paths_defaults_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "billing.json"
            path.write_text(json.dumps({"db": "a.sqlite", "pricing": "p.json"}))
            config = billing.load_config(path, default_url="http://127.0.0.1:8099")
            self.assertEqual(config["db"], Path(tmp).resolve() / "a.sqlite")
            self.assertEqual(config["public_base_url"], "http://127.0.0.1:8099")
            self.assertEqual(config["api_base_url"], "http://127.0.0.1:8099/v1")
            self.assertIsNone(config["solana"])
            path.write_text(json.dumps({"db": "a", "pricing": "p", "base": {"receiver": "x"}}))
            with self.assertRaises(billing.ConfigError):
                billing.load_config(path)
            path.write_text(json.dumps({"db": "a", "pricing": "p", "signup_grant_micro": 0.99}))
            with self.assertRaises(billing.ConfigError):
                billing.load_config(path)

    def test_public_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "billing.json"
            path.write_text(json.dumps({"db": "a", "pricing": "p",
                                        "public_base_url": "https://config.example"}))
            # GUFO_PUBLIC_URL wins over the config's optional override; slash dropped.
            config = billing.load_config(path, "https://gufo.example.com/")
            self.assertEqual((config["public_base_url"], config["api_base_url"]),
                             ("https://gufo.example.com", "https://gufo.example.com/v1"))
            config = billing.load_config(path, None, "https://api.example.com/v1/")
            self.assertEqual((config["public_base_url"], config["api_base_url"]),
                             ("https://config.example", "https://api.example.com/v1"))
            for bad in ("ftp://gufo.example.com", "gufo.example.com", "https://",
                        "https://gufo.example.com/sub", "https://u:p@gufo.example.com",
                        "https://gufo.example.com/?x=1", "https://gufo.example.com:99999"):
                with self.assertRaises(billing.ConfigError, msg=bad):
                    billing.load_config(path, bad)
            with self.assertRaises(billing.ConfigError):
                billing.load_config(path, None, "javascript:alert(1)")


class PublicUrlStartupTest(BillingHarness):
    def test_env_file_sets_the_links(self):
        base = Path(self.tmp.name)
        env = base / ".env"
        env.write_text("GUFO_PUBLIC_URL='https://tunnel.example.com/'\n")
        args = gm.parse_args(["--keys-file", str(self.keys_path), "--billing-config",
                              str(base / "billing.json"), "--log-source", "file:/dev/null",
                              "--env-file", str(env), "--port", "8100"])
        cases = (({}, "https://tunnel.example.com"),
                 ({"GUFO_PUBLIC_URL": "https://env.example.com"}, "https://env.example.com"))
        for environ, expected in cases:
            service = gm.load_billing(args, self.monitor, self.store,
                                      gm.load_environment(args.env_file, environ))
            try:
                self.assertEqual(service.verification_link("t"),
                                 expected + "/api/portal/verify?token=t")
                self.assertEqual(service.config["api_base_url"], expected + "/v1")
            finally:
                service.db.close()
        service = gm.load_billing(args, self.monitor, self.store, {})
        try:
            self.assertEqual(service.config["public_base_url"], "http://127.0.0.1:8100")
        finally:
            service.db.close()


class StartupArgsTest(unittest.TestCase):
    def test_billing_requires_a_metering_log_source(self):
        base = ["--keys-file", "keys", "--billing-config", "b.json"]
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            gm.parse_args(base)
        self.assertIn("--log-source", err.getvalue())
        self.assertEqual(gm.parse_args(base + ["--log-source", "file:/tmp/x"]).log_source,
                         "file:/tmp/x")


if __name__ == "__main__":
    unittest.main()
