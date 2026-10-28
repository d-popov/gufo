#!/usr/bin/env python3
"""CPU-only tests for tools/monitor: parsers, rates, Gufo polling and access control."""

import contextlib
import hashlib
import io
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
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
ADMIN_TOKEN = "admin-token-for-tests"


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

    def test_image_fields_kept_and_absent_on_text_requests(self):
        line = REAL_LINE.replace("finish=stop", "finish=stop images=2 image_tokens=3100 "
                                 "image_bytes=1400000 image_px=2100000 image_px_hits=1 "
                                 "image_px_misses=1 encoder_images=1 encode_ms=410.5 "
                                 "image_tokens_reused=1500")
        record = gm.request_record(gm.parse_log_line(line), show_clients=False)
        self.assertEqual({key: record[key] for key in gm.IMAGE_FIELDS}, {
            "images": 2, "image_tokens": 3100, "image_bytes": 1400000, "image_px": 2100000,
            "image_px_hits": 1, "image_px_misses": 1, "encoder_images": 1, "encode_ms": 410.5,
            "image_tokens_reused": 1500})
        text = gm.request_record(gm.parse_log_line(REAL_LINE), show_clients=False)
        self.assertFalse(set(gm.IMAGE_FIELDS) & set(text))


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
        cls.server = serve(gm.Server(("127.0.0.1", 0), cls.monitor, gm.AdminAuth(ADMIN_TOKEN),
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

    def test_requires_the_admin(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-Admin-Token": "wrong"},
                        {"Cookie": f"{gm.ADMIN_COOKIE_NAME}={ADMIN_TOKEN}"},
                        {"Cookie": "gufo_monitor=" + "0" * 64}):
            for path in ("/api/state", "/api/stream"):
                response, _ = self.request("GET", path, headers)
                self.assertEqual(response.status, 401, (path, headers))
            response, _ = self.request("GET", "/monitor", headers)
            self.assertEqual((response.status, response.getheader("Location")), (303, "/"))
            response, data = self.request("GET", "/api/admin/session", headers)
            self.assertEqual(json.loads(data), {"admin": False})

    def test_state_with_bearer_token_hides_the_gufo_key(self):
        response, data = self.request("GET", "/api/state",
                                      {"Authorization": "Bearer " + ADMIN_TOKEN})
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

    def test_one_listener_serves_portal_and_monitor_pages(self):
        response, data = self.request("GET", "/")
        self.assertEqual(response.status, 200)  # the public portal page
        self.assertIn(b'src="/static/portal.js"', data)
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
        self.assertNotIn(b"<script>", data)
        response, data = self.request("GET", "/monitor", {"X-Admin-Token": ADMIN_TOKEN})
        self.assertEqual(response.status, 200)
        self.assertIn(b'src="/static/app.js"', data)
        self.assertIn(b'src="/static/admin.js"', data)
        self.assertNotIn(b"<script>", data)
        # Without --billing-config the portal API is absent, not the monitor's.
        response, data = self.request("GET", "/api/portal/me")
        self.assertEqual((response.status, json.loads(data)["error"]), (404, "not_found"))
        for method in ("GET", "POST"):  # the old token login page is gone
            response, _ = self.request(method, "/login")
            self.assertIn(response.status, (401, 404), method)

    def test_static_whitelist(self):
        for name in ("app.css", "app.js", "admin.js", "admin.css", "header.js", "header.css",
                     "icons.js", "favicon.svg", "portal.js", "portal.css", "wallet.js"):
            response, _ = self.request("GET", "/static/" + name)
            self.assertEqual(response.status, 200, name)
        for path in ("/static/../gufo_monitor.py", "/static/%2e%2e/gufo_monitor.py",
                     "/static/index.html", "/static/", "/static/login.html"):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 404, path)

    def test_stream_sends_ticks(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/api/stream", headers={"Authorization": "Bearer " + ADMIN_TOKEN})
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
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.keys = Path(self.directory.name) / "api_keys"
        self.keys.write_text("watch sha256:" + "ab" * 32 + " access=monitor\n")
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        self.server = serve(gm.Server(("127.0.0.1", 0), monitor, gm.AdminAuth(ADMIN_TOKEN),
                                      False, gm.KeyStore(self.keys)))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.directory.cleanup()

    def request(self, method, path, headers=None, body=None):
        headers = dict(headers or {})
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

    def sign_in(self, headers=None) -> str:
        """The Set-Cookie header of a successful admin sign-in."""
        response, _ = self.request("POST", "/api/admin/login", headers, {"token": ADMIN_TOKEN})
        self.assertEqual(response.status, 204)
        cookies = response.headers.get_all("Set-Cookie")
        self.assertEqual(len(cookies), 1)  # the admin cookie only; there is no monitor cookie
        return cookies[0]

    def test_keys_are_the_admins_alone(self):
        new = {"name": "alice", "access": "inference", "models": ["fn"]}
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-Admin-Token": "wrong"},
                        {"Cookie": "gufo_monitor=" + "0" * 64}):
            for method, path, body in (("GET", "/api/keys", None), ("POST", "/api/keys", new),
                                       ("PUT", "/api/keys/watch", {"models": []}),
                                       ("DELETE", "/api/keys/watch", None)):
                response, _ = self.request(method, path, headers, body)
                self.assertEqual(response.status, 401, (method, path, headers))
        self.assertNotIn("alice", self.keys.read_text())
        for headers in ({"X-Admin-Token": ADMIN_TOKEN},
                        {"Authorization": "Bearer " + ADMIN_TOKEN}):
            response, data = self.request("GET", "/api/keys", headers)
            self.assertEqual((response.status, json.loads(data)), (200, {
                "keys": [{"name": "watch", "access": "monitor", "models": []}]}))
            self.assertNotIn(b"abab", data)  # digests stay in the file

    def test_admin_creates_edits_and_revokes(self):
        response, _ = self.request("POST", "/api/admin/login", body={"token": "wrong"})
        self.assertEqual(response.status, 401)
        cookie = self.sign_in()
        for part in ("Path=/;", "HttpOnly", "SameSite=Strict", f"Max-Age={12 * 3600}"):
            self.assertIn(part, cookie)
        self.assertEqual(gm.ADMIN_COOKIE_MAX_AGE, 12 * 3600)
        self.assertNotIn(ADMIN_TOKEN, cookie)
        admin = {"Cookie": cookie.split(";")[0]}
        response, _ = self.request("GET", "/api/keys", admin)
        self.assertEqual(response.status, 200)

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
        response, _ = self.request("DELETE", "/api/keys/alice", {"X-Admin-Token": ADMIN_TOKEN})
        self.assertEqual(response.status, 204)
        response, data = self.request("DELETE", "/api/keys/watch", admin)
        self.assertEqual(response.status, 400)
        self.assertIn(b"last key", data)
        response, _ = self.request("POST", "/api/admin/logout", admin)
        cleared = response.headers.get_all("Set-Cookie")
        self.assertEqual(len(cleared), 1)
        for part in (f"{gm.ADMIN_COOKIE_NAME}=;", "Path=/;", "Max-Age=0"):
            self.assertIn(part, cleared[0])

    def test_admin_cookie_opens_the_monitor(self):
        for path in ("/api/state", "/api/keys", "/api/stream"):
            response, _ = self.request("GET", path)
            self.assertEqual(response.status, 401, path)
        response, _ = self.request("GET", "/monitor")
        self.assertEqual((response.status, response.getheader("Location")), (303, "/"))
        # A form post cannot sign in: JSON is required.
        response, _ = self.request("POST", "/api/admin/login",
                                   {"Content-Type": "application/x-www-form-urlencoded"},
                                   "token=" + ADMIN_TOKEN)
        self.assertEqual(response.status, 401)
        cookie = self.sign_in({"X-Forwarded-Proto": "https"})
        for part in ("Path=/;", "HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(part, cookie)
        admin = {"Cookie": cookie.split(";")[0]}
        # Path=/: the browser sends the one cookie to the page and to /api/.
        response, data = self.request("GET", "/monitor", admin)
        self.assertEqual(response.status, 200)
        self.assertIn(b'src="/static/app.js"', data)
        self.assertNotIn(b"data-monitor-signout", data)
        for path in ("/api/state", "/api/keys"):
            response, _ = self.request("GET", path, admin)
            self.assertEqual(response.status, 200, path)
        response, data = self.request("GET", "/api/admin/session", admin)
        self.assertEqual(json.loads(data), {"admin": True})

    def test_admin_cookie_expires_and_cannot_be_forged(self):
        now = time.time()
        fresh = self.server.admin.session(now)
        expires, signature = fresh.split(".")
        for value in (self.server.admin.session(now - gm.ADMIN_COOKIE_MAX_AGE - 1),
                      f"{int(expires) + 3600}.{signature}", f"{expires}.{'0' * 64}",
                      "1" * 40 + "." + signature, expires, ADMIN_TOKEN,
                      gm.AdminAuth("another token").session(now)):
            response, _ = self.request("GET", "/api/state",
                                       {"Cookie": f"{gm.ADMIN_COOKIE_NAME}={value}"})
            self.assertEqual(response.status, 401, value)
        response, _ = self.request("GET", "/api/state", {"Cookie": f"{gm.ADMIN_COOKIE_NAME}={fresh}"})
        self.assertEqual(response.status, 200)

    def test_stream_ends_with_the_admin_session(self):
        cookie = self.server.admin.session(time.time() - gm.ADMIN_COOKIE_MAX_AGE + 1.5)
        headers = {"Cookie": f"{gm.ADMIN_COOKIE_NAME}={cookie}"}
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                                timeout=10)
        connection.request("GET", "/api/stream", headers=headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        started = time.monotonic()
        self.assertTrue(response.read().startswith(b"retry: 2000\n\n"))  # read to the end
        self.assertLess(time.monotonic() - started, 5)
        connection.close()
        time.sleep(max(0.0, float(cookie.split(".")[0]) - time.time()))
        response, _ = self.request("GET", "/api/stream", headers)
        self.assertEqual(response.status, 401)  # the browser's reconnect

    def test_admin_sign_in_is_rate_limited_per_client(self):
        self.assertEqual((gm.RateLimiter().limit, gm.RateLimiter().window_s), (10, 60.0))
        self.server.limiter = gm.RateLimiter(limit=3)  # each wrong token costs half a second

        def attempts(token, headers=None, count=1):
            return [self.request("POST", "/api/admin/login", headers, {"token": token})[0].status
                    for _ in range(count)]

        self.assertEqual(attempts("wrong", count=4), [401] * 3 + [429])
        self.assertEqual(attempts(ADMIN_TOKEN), [429])  # even the right token waits now
        # From loopback (a tunnel on this host), CF-Connecting-IP names the client.
        tunnel = {"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "198.51.100.1"}
        self.assertEqual(attempts("wrong", tunnel, 4), [401] * 3 + [429])
        self.assertEqual(attempts(ADMIN_TOKEN, {**tunnel, "CF-Connecting-IP": "203.0.113.8"}),
                         [204])

    def test_no_key_routes_without_a_keys_file(self):
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        plain = serve(gm.Server(("127.0.0.1", 0), monitor, gm.AdminAuth(ADMIN_TOKEN), False))
        try:
            for method in ("GET", "POST"):
                connection = http.client.HTTPConnection("127.0.0.1", plain.server_address[1],
                                                        timeout=5)
                connection.request(method, "/api/keys", headers={"X-Admin-Token": ADMIN_TOKEN})
                self.assertEqual(connection.getresponse().status, 404)
                connection.close()
        finally:
            plain.shutdown()
            plain.server_close()


class AdminOnlyFromThisHostTest(unittest.TestCase):
    """What --trust-localhost used to admit: a browser on the monitor's own host, asking
    for a loopback name with no proxy headers. It gets nothing without the admin."""

    def setUp(self):
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        self.server = serve(gm.Server(("127.0.0.1", 0), monitor, gm.AdminAuth(ADMIN_TOKEN),
                                      False))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_loopback_browser_needs_the_admin(self):
        port = self.server.server_address[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            for path in ("/api/state", "/api/stream", "/monitor"):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("GET", path, headers={"Host": host})
                response = connection.getresponse()
                self.assertEqual(response.status, 303 if path == "/monitor" else 401,
                                 (host, path))
                connection.close()

    def test_removed_options_are_rejected(self):
        for argv in (["--trust-localhost"], ["--token-file", "/tmp/token"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                gm.parse_args(argv)


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / ".env"

    def tearDown(self):
        self.directory.cleanup()

    def test_parser(self):
        self.path.write_text(
            "# the tunnel\n\n"
            "GUFO_PUBLIC_URL = https://gufo.example.com/  # trailing comment\n"
            "GUFO_API_URL='https://api.example.com/v1#not a comment'\n"
            'QUOTED="a $HOME b"  # comment\n'
            "EMPTY=\n"
            "HASH=a#b\n")
        self.assertEqual(gm.read_env_file(self.path), {
            "GUFO_PUBLIC_URL": "https://gufo.example.com/",
            "GUFO_API_URL": "https://api.example.com/v1#not a comment",
            "QUOTED": "a $HOME b",  # no shell expansion
            "EMPTY": "",
            "HASH": "a#b",
        })
        self.assertEqual(gm.read_env_file(Path(self.directory.name) / "missing"), {})
        for bad in ("export GUFO_PUBLIC_URL=x\n", "no equals sign\n", "A='unterminated\n",
                    "A='x' trailing\n", "1A=x\n"):
            self.path.write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                gm.read_env_file(self.path)

    def test_process_environment_wins(self):
        self.path.write_text("GUFO_PUBLIC_URL=https://from-file.example\n"
                             "GUFO_API_URL=https://api-file.example/v1\n")
        environ = {"GUFO_PUBLIC_URL": "https://from-env.example", "GUFO_API_URL": "",
                   "HOME": "/x"}
        self.assertEqual(gm.load_environment(self.path, environ), {
            "GUFO_PUBLIC_URL": "https://from-env.example",
            "GUFO_API_URL": "https://api-file.example/v1",  # empty in the environment: unset
        })
        self.assertEqual(gm.load_environment(Path(self.directory.name) / "missing", {}), {})

    def test_env_file_argument(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            gm.parse_args(["--env-file", str(Path(self.directory.name) / "missing")])
        self.assertIn("--env-file", err.getvalue())
        self.path.write_text("GUFO_PUBLIC_URL=https://gufo.example.com\n")
        self.assertEqual(gm.parse_args(["--env-file", str(self.path)]).env_file, self.path)
        self.assertEqual(gm.parse_args([]).env_file, gm.default_env_file())
        for removed in ("--portal-port", "--portal-bind"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                gm.parse_args([removed, "8098"])


def stats_record(t, request="r1", generated=100, decode_tps=50.0, prefill=40, prefill_tps=400.0,
                 prompt=200, cached=160, status=200, path="/v1/chat/completions", method="POST"):
    return {"request": request, "method": method, "path": path, "status": status, "t": t,
            "duration_ms": 1000.0, "prompt_tokens": prompt, "prefill_tokens": prefill,
            "cached_tokens": cached, "generated_tokens": generated, "decode_tps": decode_tps,
            "prefill_tps": prefill_tps}


def log_line(t, request, generated=10, status=200):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
    return (f"{stamp} [INFO] [http] request={request} event=completed method=POST "
            f"path=/v1/chat/completions status={status} duration_ms=500.0 outcome=completed "
            f"prompt_tokens=20 prefill_tokens=20 generated_tokens={generated} cached_tokens=0 "
            "prefill_tps=200.0 decode_tps=40.0 key=owner\n")


class StatsStoreTest(unittest.TestCase):
    HOUR = 500_000  # an hour number; NOW is half past it
    NOW = HOUR * 3600 + 1800.0

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "sub" / "stats.sqlite"
        self.stats = gm.StatsStore(self.path)

    def tearDown(self):
        self.stats.conn.close()
        self.dir.cleanup()

    def windows(self, now=None):
        return self.stats.long_window(self.NOW if now is None else now)["windows"]

    def test_file_is_private_and_wal(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.stats.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_hourly_buckets_and_window_boundaries(self):
        # The 24 h window is the current UTC hour and the 23 before it; 168 h likewise.
        for i, hours_ago in enumerate((0, 23, 24, 167, 168)):
            t = (self.HOUR - hours_ago) * 3600 + 10.0
            self.assertTrue(self.stats.add_request(stats_record(t, f"r{i}"), now=self.NOW))
        self.stats.add_request(stats_record(self.NOW - 60, "r9", status=503), now=self.NOW)
        self.stats.flush(self.NOW)
        self.stats.add_request(stats_record(self.NOW - 30, "r10"), now=self.NOW)  # pending
        day, week = self.windows()["24h"], self.windows()["168h"]
        self.assertEqual((day["requests"], week["requests"]), (4, 6))
        self.assertEqual((day["errors"], week["errors"]), (1, 1))
        self.assertEqual(day["generated_tokens"], 400)
        self.assertEqual(day["start"], (self.HOUR - 23) * 3600)
        self.assertEqual(week["start"], (self.HOUR - 167) * 3600)
        self.assertEqual(self.stats.conn.execute("SELECT count(*) FROM hours").fetchone()[0], 5)
        self.assertAlmostEqual(day["cache_reuse_pct"], 80.0)

    def test_rates_are_weighed_by_tokens(self):
        # 1000 tokens at 50 tok/s (20 s) and 10 at 10 tok/s (1 s): 1010 / 21 s, not 30.
        self.stats.add_request(stats_record(self.NOW - 10, "a", 1000, 50.0, 3000, 1000.0),
                               now=self.NOW)
        self.stats.add_request(stats_record(self.NOW - 5, "b", 10, 10.0, 100, 100.0), now=self.NOW)
        # No rate: counted as a request, left out of the rates.
        self.stats.add_request(stats_record(self.NOW - 4, "c", 0, None, 0, None), now=self.NOW)
        day = self.windows()["24h"]
        self.assertAlmostEqual(day["decode_tps"], 1010 / 21)
        self.assertAlmostEqual(day["prefill_tps"], 3100 / 4)
        self.assertEqual(day["requests"], 3)

    def test_only_inference_requests_count(self):
        for request, method, path in (("a", "GET", "/v1/models"), ("b", "POST", "/admin/x")):
            record = stats_record(self.NOW - 1, request, method=method, path=path)
            self.assertFalse(self.stats.add_request(record, now=self.NOW))
        self.assertEqual(self.windows()["24h"]["requests"], 0)

    def test_replays_and_restarts_count_once(self):
        record = stats_record(self.NOW - 100, "r7")
        self.assertTrue(self.stats.add_request(dict(record), now=self.NOW))
        self.assertFalse(self.stats.add_request(dict(record), now=self.NOW))  # pending
        self.stats.flush(self.NOW)
        self.assertFalse(self.stats.add_request(dict(record), now=self.NOW))  # stored
        # The same id at another time is another request (Gufo restarted).
        self.assertTrue(self.stats.add_request(stats_record(self.NOW - 50, "r7"), now=self.NOW))
        self.stats.flush(self.NOW)
        self.stats.conn.close()
        self.stats = gm.StatsStore(self.path)  # a monitor restart
        self.assertFalse(self.stats.add_request(dict(record), now=self.NOW))
        self.assertEqual(self.windows()["24h"]["requests"], 2)

    def test_backfill_reads_the_rotated_log_once_and_the_tail_adds_nothing(self):
        now = time.time()
        log = Path(self.dir.name) / "gufo.log"
        Path(str(log) + ".1").write_text(
            log_line(now - 7200, "r1", 5) + "garbage line\n" + log_line(now - 7100, "r2", 7))
        log.write_text(log_line(now - 3600, "r1", 11, status=500)
                       + log_line(now - 60, "r3", 13).replace("method=POST", "method=GET"))
        self.assertTrue(self.stats.needs_backfill(now))
        self.assertEqual(gm.backfill_stats(self.stats, log, log_utc=True), 3)
        self.assertEqual(gm.backfill_stats(self.stats, log, log_utc=True), 0)
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        monitor.stats = self.stats
        tail = gm.LogTail(f"file:{log}", monitor, show_clients=False, log_utc=True)
        for line in log.read_text().splitlines():
            tail.feed(line)
        self.assertEqual(len(monitor.requests), 2)  # the tail lists the file's own lines
        long = self.stats.long_window(now)
        week = long["windows"]["168h"]
        self.assertEqual((week["requests"], week["errors"], week["generated_tokens"]), (3, 1, 23))
        self.assertEqual(long["since"], float(int(now - 7200)))  # the rotated file's first line
        self.assertAlmostEqual(week["data_hours"], (now - int(now - 7200)) / 3600)
        self.assertAlmostEqual(week["tokens_per_hour"], 23 / week["data_hours"])
        self.assertTrue(self.stats.needs_backfill(now))  # the log is younger than 7 days
        self.stats.note_log_time(now - 169 * 3600)
        self.assertFalse(self.stats.needs_backfill(now))

    def test_pruning_after_eight_days(self):
        old = self.NOW - 8 * 86400 + 600
        self.assertTrue(self.stats.add_request(stats_record(old, "old"), now=self.NOW))
        self.stats.add_sample(old, {"gpu_busy_pct": 50.0})
        self.stats.flush(self.NOW)
        count = lambda table: self.stats.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        self.assertEqual((count("hours"), count("seen")), (1, 1))
        later = self.NOW + 3600
        self.stats.flush(later)
        self.assertEqual((count("hours"), count("seen")), (0, 0))
        # Older than the retention: never counted, so a replay cannot revive it.
        self.assertFalse(self.stats.add_request(stats_record(old, "old"), now=later))

    def test_gpu_samples_and_serving_time(self):
        samples = ({"running": 1, "queued": 0, "gpu_busy_pct": 10.0, "gpu_power_w": 20.0},
                   {"running": 0, "queued": 0, "gpu_busy_pct": 30.0, "gpu_power_w": 40.0,
                    "gpu_temp_c": 50.0},
                   {"running": None, "queued": 2, "gpu_busy_pct": None, "gpu_power_w": None})
        for i, sample in enumerate(samples):
            self.stats.add_sample(self.NOW - 10 + i, sample)
        self.stats.flush(self.NOW)
        self.stats.add_sample(self.NOW - 2 * 86400, {"gpu_busy_pct": 90.0})  # 168 h only
        long = self.stats.long_window(self.NOW)
        day, week = long["windows"]["24h"], long["windows"]["168h"]
        self.assertAlmostEqual(day["serving_pct"], 200 / 3)
        self.assertAlmostEqual(day["gpu_busy_pct"], 20.0)
        self.assertAlmostEqual(day["gpu_power_w"], 30.0)
        self.assertAlmostEqual(day["gpu_temp_c"], 50.0)
        self.assertAlmostEqual(day["gpu_hours"], 3 / 3600)
        self.assertAlmostEqual(week["gpu_busy_pct"], 130 / 3)
        self.assertAlmostEqual(week["serving_pct"], 50.0)
        self.assertEqual(long["gpu_since"], self.NOW - 2 * 86400)
        self.assertEqual(day["gpu_since"], day["start"])
        self.assertIsNone(day["decode_tps"])  # no requests: no rate, not zero
        self.assertIsNone(day["tokens_per_hour"])  # no log seen
        json.dumps(long, allow_nan=False)

    def test_image_sums(self):
        image = {"images": 2, "image_tokens": 3000, "encoder_images": 1, "encode_ms": 400.0,
                 "image_tokens_reused": 1500, "image_bytes": 999}
        self.stats.add_request({**stats_record(self.NOW - 30, "a"), **image}, now=self.NOW)
        self.stats.flush(self.NOW)
        # Without encode_ms: an image request, left out of the per-request encoder time.
        self.stats.add_request({**stats_record(self.NOW - 20, "b"), "images": 1,
                                "image_tokens": 1000}, now=self.NOW)
        self.stats.add_request({**stats_record(self.NOW - 10, "c"), "images": 1,
                                "encode_ms": 100.0, "encoder_images": 1}, now=self.NOW)
        self.stats.add_request(stats_record(self.NOW - 5, "text"), now=self.NOW)
        day = self.windows()["24h"]
        self.assertEqual((day["requests"], day["image_requests"], day["images"],
                          day["image_tokens"], day["encoder_images"], day["image_tokens_reused"]),
                         (4, 3, 4, 4000, 2, 1500))
        self.assertAlmostEqual(day["encode_ms"], 500.0)
        self.assertAlmostEqual(day["encode_ms_per_request"], 250.0)
        self.assertNotIn("image_bytes", day)

    def test_text_only_windows_have_no_encoder_time(self):
        self.stats.add_request(stats_record(self.NOW - 5, "text"), now=self.NOW)
        day = self.windows()["24h"]
        self.assertEqual((day["image_requests"], day["images"]), (0, 0))
        self.assertIsNone(day["encode_ms_per_request"])

    def test_migrates_a_store_without_image_columns(self):
        self.stats.conn.close()
        old = Path(self.dir.name) / "old.sqlite"
        legacy = gm.STATS_SUMS[:gm.STATS_SUMS.index("image_requests")]
        conn = gm.sqlite3.connect(str(old))
        conn.executescript(
            "CREATE TABLE hours (hour INTEGER PRIMARY KEY, "
            + ", ".join(f"{name} REAL NOT NULL DEFAULT 0" for name in legacy) + ");\n"
            + gm.STATS_SCHEMA.split(";\n", 1)[1])
        conn.execute("INSERT INTO hours (hour, requests, generated_tokens) VALUES (?, 3, 30)",
                     (self.HOUR,))
        conn.commit()
        conn.close()
        self.stats = gm.StatsStore(old)
        columns = [row[1] for row in self.stats.conn.execute("PRAGMA table_info(hours)")]
        self.assertEqual(columns, ["hour", *gm.STATS_SUMS])
        day = self.windows()["24h"]
        self.assertEqual((day["requests"], day["generated_tokens"]), (3, 30))  # kept
        self.assertEqual((day["image_requests"], day["encode_ms"]), (0, 0.0))  # old rows: 0
        self.assertIsNone(day["encode_ms_per_request"])
        self.stats.add_request({**stats_record(self.NOW - 5, "img"), "images": 1,
                                "encode_ms": 80.0}, now=self.NOW)
        self.stats.flush(self.NOW)
        self.stats.conn.close()
        self.stats = gm.StatsStore(old)  # migrated already: nothing to add
        columns = [row[1] for row in self.stats.conn.execute("PRAGMA table_info(hours)")]
        self.assertEqual(columns, ["hour", *gm.STATS_SUMS])
        day = self.windows()["24h"]
        self.assertEqual((day["requests"], day["image_requests"]), (4, 1))
        self.assertAlmostEqual(day["encode_ms_per_request"], 80.0)

    def test_monitor_feeds_the_store(self):
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        monitor.stats = self.stats
        now = time.time()
        monitor.poll_once(now)  # Gufo is down: still a (not serving) sample
        monitor.add_request(stats_record(now - 1, "r5"))
        day = self.stats.long_window(now)["windows"]["24h"]
        self.assertEqual((day["requests"], day["serving_pct"]), (1, 0.0))


class LongStatsHttpTest(unittest.TestCase):
    def setUp(self):
        self.monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        self.server = serve(gm.Server(("127.0.0.1", 0), self.monitor, gm.AdminAuth(ADMIN_TOKEN),
                                      False))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1],
                                                timeout=5)
        connection.request("GET", "/api/stats/long", headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_admin_only_and_shape(self):
        admin = {"X-Admin-Token": ADMIN_TOKEN}
        self.assertEqual(self.get(admin)[0], 404)  # no store
        self.monitor.stats = gm.StatsStore(":memory:")
        self.monitor.stats.add_request(stats_record(time.time() - 60))
        for headers in ({}, {"X-Admin-Token": "wrong"}, {"Authorization": "Bearer wrong"}):
            self.assertEqual(self.get(headers), (401, b'{"error":"unauthorized"}'))
        status, data = self.get(admin)
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertEqual(set(body["windows"]), {"24h", "168h"})
        self.assertEqual(body["windows"]["24h"]["requests"], 1)
        self.assertEqual(body["windows"]["168h"]["decode_tps"], 50.0)
        status, _ = self.get({"Authorization": "Bearer " + ADMIN_TOKEN})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
