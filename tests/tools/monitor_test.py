#!/usr/bin/env python3
"""CPU-only tests for tools/monitor: parsers, rates, Gufo polling and access control."""

import hashlib
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("gufo_monitor", ROOT / "tools/monitor/gufo-monitor.py")
gm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gm)

REAL_LINE = (
    "2026-10-23 20:12:28 [INFO] [http] request=r2 event=completed method=POST "
    "path=/v1/chat/completions status=200 duration_ms=329.1 outcome=completed prompt_tokens=28 "
    "prefill_tokens=28 generated_tokens=3 finish=stop cache=miss cached_tokens=0 "
    "cache_restore_ms=0.0 queue_depth=1 resident_at_admission=1 queue_ms=1.5 ttft_ms=246.4 "
    "prefill_tps=114.4 decode_tps=39.1 batch_width=1 plan=serial-fallback draft_accepted=2 "
    "draft_proposed=3 acceptance_pct=66.7 rss_mib=1234 host_available_mib=90000"
)
GUFO_KEY = "gufo-secret-key-do-not-leak-7f3a"
MONITOR_TOKEN = "monitor-token-for-tests"


class PrometheusParserTest(unittest.TestCase):
    def test_parses_values_and_skips_noise(self):
        text = (
            "# HELP llamacpp:prompt_tokens_total Total\n"
            "# TYPE llamacpp:prompt_tokens_total counter\n"
            "llamacpp:prompt_tokens_total 1234\n"
            "llamacpp:kv_cache_usage_ratio 0.25\n"
            'labelled{kind="a b",x="1"} 7 1700000000\n'
            "not_a_number abc\n"
            "nan_value NaN\n"
            "no_value\n"
            "\n"
        )
        self.assertEqual(gm.parse_prometheus(text), {
            "llamacpp:prompt_tokens_total": 1234.0,
            "llamacpp:kv_cache_usage_ratio": 0.25,
            'labelled{kind="a b",x="1"}': 7.0,
        })


class LogParserTest(unittest.TestCase):
    def test_real_completion_line(self):
        entry = gm.parse_log_line(REAL_LINE, log_utc=True)
        self.assertEqual(entry["component"], "http")
        self.assertEqual(entry["level"], "INFO")
        self.assertEqual(entry["t"], 1792786348.0)  # 2026-10-23 20:12:28 UTC
        record = gm.request_record(entry, show_clients=False)
        self.assertEqual(record["status"], 200)
        self.assertEqual(record["prompt_tokens"], 28)
        self.assertEqual(record["decode_tps"], 39.1)
        self.assertEqual(record["acceptance_pct"], 66.7)
        self.assertEqual(record["plan"], "serial-fallback")
        self.assertNotIn("rss_mib", record)

    def test_unknown_keys_are_tolerated_and_dropped(self):
        line = REAL_LINE.replace("finish=stop", "finish=stop future_key=xyz client_id=10.0.0.5")
        entry = gm.parse_log_line(line)
        self.assertEqual(entry["fields"]["future_key"], "xyz")
        hidden = gm.request_record(entry, show_clients=False)
        self.assertNotIn("future_key", hidden)
        self.assertNotIn("client", hidden)
        self.assertEqual(gm.request_record(entry, show_clients=True)["client"], "10.0.0.5")

    def test_podman_timestamp_prefix_and_colour(self):
        line = "2026-10-23T23:12:28.123456789+03:00 2026-10-23 20:12:28 " + REAL_LINE[20:]
        entry = gm.parse_log_line(line)
        self.assertAlmostEqual(entry["t"], 1792786348.123456, places=5)
        coloured = "2026-10-23 20:12:28 \x1b[32m[INFO]\x1b[0m [http] request=r1 event=received"
        self.assertEqual(gm.parse_log_line(coloured)["fields"]["event"], "received")

    def test_non_request_lines(self):
        self.assertIsNone(gm.parse_log_line("Error loading model 'x': std::bad_alloc"))
        poll = REAL_LINE.replace("path=/v1/chat/completions", "path=/metrics")
        self.assertIsNone(gm.request_record(gm.parse_log_line(poll), show_clients=False))
        received = "2026-10-23 20:12:28 [INFO] [http] request=r1 event=received path=/v1/x"
        self.assertIsNone(gm.request_record(gm.parse_log_line(received), show_clients=False))


class RatesTest(unittest.TestCase):
    def test_counter_reset_starts_a_new_baseline(self):
        rates = gm.CounterRates()
        name = "llamacpp:tokens_predicted_total"
        self.assertIsNone(rates.update({name: 100}, 1.0))
        self.assertEqual(rates.update({name: 160}, 2.0)["predicted"], 60)
        self.assertIsNone(rates.update({name: 5}, 3.0))  # Gufo restarted
        self.assertEqual(rates.update({name: 25}, 4.0)["predicted"], 20)

    def test_decode_rate_spans_the_task_window(self):
        rates = gm.SlotRates(window_s=5.0)

        def rate(task, decoded, processed, t, busy=True):
            slot = {"id": 0, "model": "m", "is_processing": busy, "id_task": task,
                    "n_prompt_tokens_processed": processed, "next_token": [{"n_decoded": decoded}]}
            return rates.update([slot], t)[("m", 0)]

        self.assertEqual(rate(-1, 0, 0, 0.0, busy=False), (0.0, 0.0))
        self.assertEqual(rate(1, 0, 1000, 1.0), (None, 1000.0))  # task started after the last poll
        self.assertEqual(rate(1, 40, 1000, 2.0), (None, 0.0))  # prefill over; first tokens
        self.assertEqual(rate(1, 70, 1000, 3.0)[0], None)  # 1 s since the first token: too noisy
        self.assertEqual(rate(1, 110, 1000, 4.0)[0], None)
        self.assertAlmostEqual(rate(1, 140, 1000, 5.0)[0], 100 / 3)  # since 2 s: prefill excluded
        self.assertEqual(rate(1, 180, 1000, 6.0)[0], 35.0)
        self.assertEqual(rate(1, 285, 1000, 9.0)[0], 35.0)  # window slid to (4 s, 110)
        self.assertEqual(rate(2, 5, 200, 10.0), (None, 200.0))  # a new task starts a new window
        self.assertEqual(rate(-1, 5, 200, 11.0, busy=False), (0.0, 0.0))


class FakeGufo(http.server.BaseHTTPRequestHandler):
    """Gufo stand-in requiring GUFO_KEY; counters are mutable class state."""

    counters = {"prompt": 1000, "cached": 3000, "predicted": 500, "draft": 100, "accepted": 70}
    ready_status = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + GUFO_KEY:
            return self._reply(401, {"error": {"code": "invalid_api_key"}})
        path = self.path.split("?")[0]
        if path == "/ready":
            if self.ready_status != 200:
                return self._reply(503, {"error": {"code": "loading"}})
            return self._reply(200, {"status": "ready", "model": "fn"})
        if path == "/v1/models":
            return self._reply(200, {"data": [{"id": "fn", "context_length": 262144},
                                              {"id": "fn-sub", "context_length": 65536}]})
        if path == "/props":
            model = self.path.partition("model=")[2] or "fn"
            return self._reply(200, {"model_alias": model, "build_info": "0.22.0 (abc)",
                                     "total_slots": 1,
                                     "default_generation_settings": {"n_ctx": 65536}})
        if path == "/slots":
            return self._reply(200, [
                {"id": 0, "model": "fn", "n_ctx": 262144, "is_processing": True, "id_task": 3,
                 "n_prompt_tokens": 1000, "n_prompt_tokens_cache": 900,
                 "n_prompt_tokens_processed": 100, "prompt": "",
                 "next_token": [{"n_decoded": self.counters["predicted"], "n_remain": 10}]},
                {"id": 1, "model": "fn-sub", "n_ctx": 65536, "is_processing": False,
                 "id_task": -1, "n_prompt_tokens": 0, "next_token": [{"n_decoded": 0}]},
            ])
        if path == "/metrics":
            c = self.counters
            text = (f"llamacpp:prompt_tokens_total {c['prompt']}\n"
                    f"llamacpp:prompt_tokens_cached_total {c['cached']}\n"
                    f"llamacpp:tokens_predicted_total {c['predicted']}\n"
                    f"llamacpp:spec_decode_num_draft_tokens_total {c['draft']}\n"
                    f"llamacpp:spec_decode_num_accepted_tokens_total {c['accepted']}\n"
                    "gufo_device_lost_total 0\nllamacpp:requests_processing 1\n"
                    "llamacpp:requests_deferred 2\nllamacpp:kv_cache_usage_ratio 0.5\n")
            return self._reply(200, text.encode(), "text/plain")
        self._reply(404, {})

    def _reply(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class NoTelemetry:
    def read(self):
        return {}


def serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class MonitorHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gufo = serve(http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGufo))
        gufo_url = f"http://127.0.0.1:{cls.gufo.server_address[1]}"
        cls.monitor = gm.Monitor(gm.GufoClient(gufo_url, GUFO_KEY), NoTelemetry(), poll_s=1.0,
                                 history_s=60.0)
        for t in (100.0, 101.0, 102.0, 103.0):  # +50 of every counter per second
            cls.monitor.poll_once(t)
            FakeGufo.counters = {k: v + 50 for k, v in FakeGufo.counters.items()}
        cls.monitor.add_request(gm.request_record(gm.parse_log_line(REAL_LINE), False))
        cls.server = serve(gm.Server(("127.0.0.1", 0), cls.monitor, gm.Auth(MONITOR_TOKEN),
                                     secure_cookie=False))
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.stop.set()
        with cls.monitor.cond:
            cls.monitor.cond.notify_all()
        for server in (cls.server, cls.gufo):
            server.shutdown()
            server.server_close()

    def request(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def test_requires_monitor_token(self):
        response, _ = self.request("GET", "/api/state")
        self.assertEqual(response.status, 401)
        response, _ = self.request("GET", "/api/state", {"Authorization": "Bearer wrong"})
        self.assertEqual(response.status, 401)
        response, _ = self.request("GET", "/api/stream")
        self.assertEqual(response.status, 401)
        response, _ = self.request("GET", "/")
        self.assertEqual((response.status, response.getheader("Location")), (303, "/login"))
        response, _ = self.request("GET", "/api/state", {"Cookie": f"{gm.COOKIE_NAME}={MONITOR_TOKEN}"})
        self.assertEqual(response.status, 401)  # the cookie carries a derived session, not the token

    def test_state_with_bearer_token_hides_the_gufo_key(self):
        response, data = self.request("GET", "/api/state",
                                      {"Authorization": "Bearer " + MONITOR_TOKEN})
        self.assertEqual(response.status, 200)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
        self.assertNotIn(GUFO_KEY.encode(), data)
        state = json.loads(data)
        self.assertEqual(state["status"]["state"], "ready")
        self.assertEqual([m["id"] for m in state["models"]], ["fn", "fn-sub"])
        self.assertEqual({s["model"]: s["n_ctx"] for s in state["slots"]},
                         {"fn": 262144, "fn-sub": 65536})
        self.assertEqual(state["slots"][0]["decode_tps"], 50.0)
        sample = state["sample"]
        self.assertEqual(sample["decode_tps"], 50.0)
        self.assertEqual(sample["cache_reuse_pct"], 50.0)  # 50 cached of 100
        self.assertEqual(sample["accept_pct"], 100.0)
        self.assertEqual((sample["running"], sample["queued"]), (1, 2))
        self.assertEqual(state["requests"][0]["decode_tps"], 39.1)
        self.assertEqual(len(state["history"]["t"]), 4)

    def test_login_sets_a_strict_cookie(self):
        response, data = self.request("POST", "/login", {
            "Content-Type": "application/x-www-form-urlencoded"}, "token=wrong")
        self.assertEqual(response.status, 401)
        self.assertIn(b"Wrong token", data)
        response, _ = self.request("POST", "/login", {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Forwarded-Proto": "https"}, "token=" + MONITOR_TOKEN)
        self.assertEqual(response.status, 303)
        cookie = response.getheader("Set-Cookie")
        for part in ("HttpOnly", "SameSite=Strict", "Secure", "Path=/"):
            self.assertIn(part, cookie)
        self.assertNotIn(MONITOR_TOKEN, cookie)
        session = cookie.split(";")[0]
        response, _ = self.request("GET", "/api/state", {"Cookie": "other=1; " + session})
        self.assertEqual(response.status, 200)
        response, data = self.request("GET", "/", {"Cookie": session})
        self.assertEqual(response.status, 200)
        self.assertIn(b'src="/static/app.js"', data)
        self.assertNotIn(b"<script>", data)

    def test_static_whitelist(self):
        response, _ = self.request("GET", "/static/app.css")
        self.assertEqual(response.status, 200)
        for path in ("/static/../gufo_monitor.py", "/static/%2e%2e/gufo_monitor.py",
                     "/static/index.html", "/static/"):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 404, path)

    def test_stream_sends_ticks(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/api/stream", headers={"Authorization": "Bearer " + MONITOR_TOKEN})
        response = connection.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(response.readline(), b"retry: 2000\n")
        response.readline()
        self.monitor.poll_once(104.0)
        self.assertEqual(response.readline(), b"event: tick\n")
        data = response.readline()
        self.assertTrue(data.startswith(b"data: {"))
        self.assertNotIn(GUFO_KEY.encode(), data)
        self.assertEqual(json.loads(data[6:])["status"]["state"], "ready")
        connection.close()


class GufoStatusTest(unittest.TestCase):
    def test_wrong_key_unreachable_and_loading(self):
        gufo = serve(http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGufo))
        url = f"http://127.0.0.1:{gufo.server_address[1]}"
        try:
            monitor = gm.Monitor(gm.GufoClient(url, "wrong"), NoTelemetry())
            monitor.poll_once(1.0)
            self.assertEqual(monitor.status["state"], "down")
            self.assertIn("API key", monitor.status["detail"])
            FakeGufo.ready_status = 503
            monitor = gm.Monitor(gm.GufoClient(url, GUFO_KEY), NoTelemetry())
            monitor.poll_once(1.0)
            self.assertEqual(monitor.status["state"], "loading")
            self.assertEqual(monitor.slots, [])
        finally:
            FakeGufo.ready_status = 200
            gufo.shutdown()
            gufo.server_close()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        monitor = gm.Monitor(gm.GufoClient(f"http://127.0.0.1:{closed}", None), NoTelemetry())
        monitor.poll_once(1.0)
        self.assertEqual(monitor.status["state"], "down")
        json.loads(monitor.snapshot())


class TokenFileTest(unittest.TestCase):
    def test_created_private_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sub" / "token"
            token = gm.load_or_create_token(path)
            self.assertGreaterEqual(len(token), 40)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(gm.load_or_create_token(path), token)


class KeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "keys" / "api_keys"
        self.store = gm.KeyStore(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_create_update_delete_keep_other_lines(self):
        self.path.parent.mkdir()
        self.path.write_text("# managed by hand\nold sha256:" + "AB" * 32 + " access=monitor\n")
        os.chmod(self.path, 0o640)
        token = self.store.create("alice", "inference", ["fn-sub"])
        digest = hashlib.sha256(token.encode()).hexdigest()
        text = self.path.read_text()
        self.assertEqual(text, "# managed by hand\nold sha256:" + "AB" * 32 +
                         " access=monitor\nalice sha256:" + digest + " models=fn-sub\n")
        self.assertNotIn(token, text)  # only the digest is stored
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o640)
        self.store.update("alice", "inference", [])
        self.assertIn("alice sha256:" + digest + "\n", self.path.read_text())
        self.assertEqual(self.store.list(), [
            {"name": "old", "access": "monitor", "models": []},
            {"name": "alice", "access": "inference", "models": []}])
        self.store.delete("old")
        self.assertEqual(self.path.read_text(), "# managed by hand\nalice sha256:" + digest + "\n")
        with self.assertRaisesRegex(gm.KeyFileError, "last key"):
            self.store.delete("alice")
        with self.assertRaises(LookupError):
            self.store.update("nobody", "inference", [])
        self.assertEqual(os.listdir(self.path.parent), ["api_keys"])  # no temp files left

    def test_creates_a_private_file(self):
        self.store.create("first", "monitor", [])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_rejects_what_gufo_would_reject(self):
        self.store.create("alice", "inference", [])
        for name, access, models, message in (
                ("alice", "inference", [], "already exists"),
                ("al ice", "inference", [], "key name"),
                ("x" * 65, "inference", [], "key name"),
                ("bob", "admin", [], "access"),
                ("bob", "monitor", ["fn"], "monitor key"),
                ("bob", "inference", ["a b"], "spaces or commas"),
                ("bob", "inference", ["a,b"], "spaces or commas"),
                ("bob", "inference", [""], "spaces or commas"),
                ("bob", "inference", "fn", "required"),
                (None, "inference", [], "required")):
            with self.assertRaisesRegex(gm.KeyFileError, message):
                self.store.create(name, access, models)
        self.assertEqual([k["name"] for k in self.store.list()], ["alice"])

    def test_parser_matches_gufo_rules(self):
        for line in ("alice sha256:abc", "alice plain", "alice sha256:" + "a" * 64 + " models=",
                     "alice sha256:" + "a" * 64 + " models=a,", "alice sha256:" + "a" * 64 + " x=1",
                     "alice sha256:" + "a" * 64 + " access=monitor access=monitor"):
            with self.assertRaises(gm.KeyFileError, msg=line):
                gm.parse_key_line(line)
        self.assertIsNone(gm.parse_key_line("   # comment"))


class KeyUsageTest(unittest.TestCase):
    def test_usage_per_key_and_reload_status(self):
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        tail = gm.LogTail("file:/dev/null", monitor, show_clients=False, log_utc=True)
        tail.feed(REAL_LINE.replace("rss_mib=", "key=007 rss_mib="))
        tail.feed(REAL_LINE.replace("request=r2", "request=r3").replace("status=200", "status=429")
                  .replace("rss_mib=", "key=007 rss_mib="))
        tail.feed("2026-10-23 20:12:30 [INFO] [server] event=api_keys_reloaded keys=4")
        self.assertEqual(monitor.requests[0]["key"], "007")
        usage = monitor.key_usage["007"]
        self.assertEqual((usage["requests"], usage["errors"], usage["prompt_tokens"],
                          usage["generated_tokens"]), (2, 1, 56, 6))
        self.assertEqual(monitor.key_reload, {"t": 1792786350.0, "ok": True, "keys": 4})
        tail.feed("2026-10-23 20:12:31 [ERROR] [server] event=api_keys_reload_failed "
                  "reason=/keys/api_keys: line 2: access must be inference or monitor")
        self.assertEqual(monitor.key_reload["reason"],
                         "/keys/api_keys: line 2: access must be inference or monitor")
        self.assertFalse(monitor.key_reload["ok"])


class KeyAdminHttpTest(unittest.TestCase):
    ADMIN = "admin-token-for-tests"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.keys = Path(self.directory.name) / "api_keys"
        self.keys.write_text("watch sha256:" + "ab" * 32 + " access=monitor\n")
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        admin = gm.Auth(self.ADMIN, gm.ADMIN_COOKIE_NAME, b"gufo-monitor-admin-v1",
                        "X-Admin-Token")
        self.server = serve(gm.Server(("127.0.0.1", 0), monitor, gm.Auth(MONITOR_TOKEN),
                                      False, gm.KeyStore(self.keys), admin))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def request(self, method, path, headers=None, body=None, monitor=True):
        headers = dict(headers or {})
        if monitor:
            headers["Authorization"] = "Bearer " + MONITOR_TOKEN
        if isinstance(body, dict):
            body = json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                                timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def test_viewing_needs_the_monitor_token_and_changes_the_admin_token(self):
        response, _ = self.request("GET", "/api/keys", monitor=False)
        self.assertEqual(response.status, 401)
        response, data = self.request("GET", "/api/keys")
        self.assertEqual(json.loads(data), {
            "keys": [{"name": "watch", "access": "monitor", "models": []}], "admin": False})
        self.assertNotIn(b"abab", data)  # digests stay in the file
        new = {"name": "alice", "access": "inference", "models": ["fn"]}
        response, _ = self.request("POST", "/api/keys", body=new)
        self.assertEqual(response.status, 403)
        response, _ = self.request("DELETE", "/api/keys/watch")
        self.assertEqual(response.status, 403)
        # The admin token alone is not enough either.
        response, _ = self.request("POST", "/api/keys", {"X-Admin-Token": self.ADMIN}, new,
                                   monitor=False)
        self.assertEqual(response.status, 401)
        self.assertNotIn("alice", self.keys.read_text())

    def test_admin_creates_edits_and_revokes(self):
        response, _ = self.request("POST", "/api/admin/login", body={"token": "wrong"})
        self.assertEqual(response.status, 401)
        response, _ = self.request("POST", "/api/admin/login", body={"token": self.ADMIN})
        self.assertEqual(response.status, 204)
        cookie = response.getheader("Set-Cookie")
        for part in ("Path=/api/", "HttpOnly", "SameSite=Strict", "Max-Age=3600"):
            self.assertIn(part, cookie)
        self.assertNotIn(self.ADMIN, cookie)
        admin = {"Cookie": cookie.split(";")[0]}
        _, data = self.request("GET", "/api/keys", admin)
        self.assertTrue(json.loads(data)["admin"])

        response, data = self.request("POST", "/api/keys", admin,
                                      {"name": "alice", "models": ["fn-sub"]})
        self.assertEqual(response.status, 201)
        token = json.loads(data)["token"]
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.assertIn(f"alice sha256:{digest} models=fn-sub\n", self.keys.read_text())
        # A form post cannot change keys: JSON is required.
        response, _ = self.request("POST", "/api/keys",
                                   {**admin, "Content-Type": "application/x-www-form-urlencoded"},
                                   "name=mallory")
        self.assertEqual(response.status, 400)
        response, data = self.request("POST", "/api/keys", admin, {"name": "bad name"})
        self.assertEqual(response.status, 400)
        self.assertIn(b"key name", data)
        response, _ = self.request("PUT", "/api/keys/alice", admin,
                                   {"access": "monitor", "models": []})
        self.assertEqual(response.status, 204)
        self.assertIn(f"alice sha256:{digest} access=monitor\n", self.keys.read_text())
        response, _ = self.request("PUT", "/api/keys/nobody", admin, {"models": []})
        self.assertEqual(response.status, 404)
        response, _ = self.request("DELETE", "/api/keys/alice", {"X-Admin-Token": self.ADMIN})
        self.assertEqual(response.status, 204)
        response, data = self.request("DELETE", "/api/keys/watch", admin)
        self.assertEqual(response.status, 400)
        self.assertIn(b"last key", data)
        response, _ = self.request("POST", "/api/admin/logout", admin)
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))

    def test_no_key_routes_without_a_keys_file(self):
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        plain = serve(gm.Server(("127.0.0.1", 0), monitor, gm.Auth(MONITOR_TOKEN), False))
        try:
            for method in ("GET", "POST"):
                connection = http.client.HTTPConnection("127.0.0.1", plain.server_address[1],
                                                        timeout=5)
                connection.request(method, "/api/keys",
                                   headers={"Authorization": "Bearer " + MONITOR_TOKEN})
                self.assertEqual(connection.getresponse().status, 404)
                connection.close()
        finally:
            plain.shutdown()
            plain.server_close()


if __name__ == "__main__":
    unittest.main()
