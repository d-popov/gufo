#!/usr/bin/env python3
"""gufo-monitor: a web monitor for a running `gufo serve llm`.

One background thread polls Gufo (/ready, /metrics, /slots; /v1/models and
/props less often) and the GPU/host telemetry in sysfs and /proc, another
tails Gufo's log for completed requests. Browsers read the shared state from
GET /api/state (snapshot with history) and GET /api/stream (Server-Sent
Events, one event per poll), so any number of browsers cost one set of polls.
Hourly sums of requests, image work and GPU samples persist in --stats-file for the
last 24 h / 7 days tile (GET /api/stats/long).

The Gufo API key stays in this process: it is never sent to browsers, written
to responses or logged. The monitor is for the admin only: every monitor route
needs the admin token (header) or the admin session cookie from signing in with
it, which also manages Gufo's named API keys (--keys-file) and portal accounts.
One listener serves everything: the monitor page at /monitor, and with
--billing-config the public credits portal at / and /api/portal/*.

Python 3.11+ standard library only.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import http.server
import ipaddress
import json
import math
import os
import re
import secrets
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
PORTAL_STATIC_DIR = HERE / "portal_static"
_JS, _CSS = "text/javascript; charset=utf-8", "text/css; charset=utf-8"
# /static/NAME, from either page's directory; a whitelist, so no paths or traversal.
STATIC_FILES = {
    "app.js": (STATIC_DIR, _JS),
    "app.css": (STATIC_DIR, _CSS),
    "admin.js": (STATIC_DIR, _JS),
    "admin.css": (STATIC_DIR, _CSS),
    "header.js": (STATIC_DIR, _JS),
    "header.css": (STATIC_DIR, _CSS),
    "portal.js": (PORTAL_STATIC_DIR, _JS),
    "wallet.js": (PORTAL_STATIC_DIR, _JS),
    "portal.css": (PORTAL_STATIC_DIR, _CSS),
}
# Settings read from the environment or --env-file.
ENV_KEYS = ("GUFO_PUBLIC_URL", "GUFO_API_URL")
# The admin session cookie, from POST /api/admin/login; sent to /monitor and /api/.
ADMIN_COOKIE_NAME = "gufo_monitor_admin"
ADMIN_COOKIE_MAX_AGE = 12 * 3600
SIGNIN_LIMIT = 10  # sign-in attempts per client address per SIGNIN_WINDOW_S
SIGNIN_WINDOW_S = 60.0
MAX_STREAMS = 32
MAX_REQUESTS = 500
CARD_WINDOW_S = 300.0
RATE_WINDOW_S = 30.0
MIN_RATE_SPAN_S = 3.0

SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
     "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
     "form-action 'self'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)

# Series recorded once per poll; the page draws one sparkline per key.
SERIES = (
    "decode_tps", "prefill_tps", "running", "queued", "cache_reuse_pct", "accept_pct",
    "gpu_busy_pct", "gtt_used_gb", "mem_avail_gb", "swap_used_gb", "gpu_temp_c",
    "gpu_power_w", "cpu_pct",
)

# Gufo counters; a drop in any of them means Gufo restarted.
COUNTERS = {
    "prompt": "llamacpp:prompt_tokens_total",
    "cached": "llamacpp:prompt_tokens_cached_total",
    "predicted": "llamacpp:tokens_predicted_total",
    "draft": "llamacpp:spec_decode_num_draft_tokens_total",
    "accepted": "llamacpp:spec_decode_num_accepted_tokens_total",
    "device_lost": "gufo_device_lost_total",
}

# Per-request image fields of the completed line, present only when the request had
# images. image_px_hits/misses count the CPU pixel cache (decode and resize) only: a hit
# can still run the vision encoder. encoder_images and image_tokens_reused are the
# encoder work done and the visual tokens served by continuation (KV) reuse.
IMAGE_FIELDS = (
    "images", "image_tokens", "image_bytes", "image_px", "image_px_hits", "image_px_misses",
    "encoder_images", "encode_ms", "image_tokens_reused",
)

# Completed-request fields copied from the log; everything else is dropped so
# unknown or sensitive keys never reach the page.
REQUEST_FIELDS = (
    "request", "model", "method", "path", "status", "outcome", "duration_ms", "prompt_tokens",
    "prefill_tokens", "cached_tokens", "generated_tokens", "finish", "cache", "queue_ms",
    "ttft_ms", "prefill_tps", "decode_tps", "batch_width", "plan", "draft_accepted",
    "draft_proposed", "acceptance_pct",
    # Only on requests with images; text requests and older Gufo builds have none.
    *IMAGE_FIELDS,
)
CLIENT_FIELDS = ("client", "client_id")
# Probes and monitoring routes are not requests worth listing.
POLL_PATHS = frozenset({
    "/health", "/v1/health", "/healthz", "/ready", "/v1/ready", "/readyz", "/metrics",
    "/slots", "/v1/slots", "/props", "/v1/props", "/models", "/v1/models",
})


# --------------------------------------------------------------------------- parsers

def parse_prometheus(text: str) -> dict[str, float]:
    """Parse Prometheus text exposition into {series: value}.

    Labelled series keep their label block in the key. Comments, blank lines
    and malformed lines are skipped.
    """
    out: dict[str, float] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "{" in line:
            close = line.rfind("}")
            if close < 0:
                continue
            name, rest = line[:close + 1], line[close + 1:].split()
        else:
            parts = line.split()
            name, rest = parts[0], parts[1:]
        if not rest:
            continue
        try:
            value = float(rest[0])
        except ValueError:
            continue
        if math.isfinite(value):
            out[name] = value
    return out


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PODMAN_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)\s+")
_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)?\s+\[(?P<level>[A-Z]+)\]\s+"
    r"\[(?P<component>[^\]]*)\]\s?(?P<message>.*)$")
_FIELD = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_.]*)=(\S*)")
_KEY_FIELD = re.compile(r"(?:^|\s)key=(\S+)")


def _number(value: str):
    try:
        return int(value)
    except ValueError:
        pass
    try:
        number = float(value)
    except ValueError:
        return value
    return number if math.isfinite(number) else None


def parse_log_line(line: str, log_utc: bool = False) -> dict | None:
    """Parse one Gufo log line, optionally prefixed by `podman logs --timestamps`.

    Returns {"t", "level", "component", "fields"} or None for lines that are
    not structured Gufo log lines. `t` comes from the podman prefix when there
    is one (exact, zoned), otherwise from Gufo's own timestamp, which is the
    process's local time (UTC in most containers; see --log-utc).
    """
    line = _ANSI.sub("", line.rstrip("\r\n"))
    t = None
    prefix = _PODMAN_TS.match(line)
    if prefix:
        fraction = (prefix[2] or ".0")[:7]
        zone = "+00:00" if prefix[3] == "Z" else prefix[3]
        if len(zone) == 5:
            zone = zone[:3] + ":" + zone[3:]
        try:
            t = datetime.fromisoformat(prefix[1] + fraction + zone).timestamp()
        except ValueError:
            t = None
        line = line[prefix.end():]
    match = _LINE.match(line)
    if not match:
        return None
    if t is None:
        stamp = datetime.strptime(match["ts"], "%Y-%m-%d %H:%M:%S")
        if log_utc:
            stamp = stamp.replace(tzinfo=timezone.utc)
        t = stamp.timestamp()
    fields = {key: _number(value) for key, value in _FIELD.findall(match["message"])}
    return {"t": t, "level": match["level"], "component": match["component"], "fields": fields,
            "message": match["message"]}


def key_reload_status(entry: dict) -> dict | None:
    """Gufo's view of its --api-key-file, from startup and reload lines."""
    fields = entry["fields"]
    event = fields.get("event")
    if entry["component"] != "server":
        return None
    if event == "listening" and fields.get("auth") == "key_file":
        return {"t": entry["t"], "ok": True, "keys": fields.get("keys")}
    if event == "api_keys_reloaded":
        return {"t": entry["t"], "ok": True, "keys": fields.get("keys")}
    if event == "api_keys_reload_failed":
        reason = entry["message"].partition("reason=")[2]
        return {"t": entry["t"], "ok": False, "reason": reason[:300]}
    return None


def request_record(entry: dict, show_clients: bool) -> dict | None:
    """Turn a parsed `event=completed` line into a request row, or None."""
    fields = entry["fields"]
    if entry["component"] != "http" or fields.get("event") != "completed":
        return None
    path = fields.get("path")
    if not isinstance(path, str) or path.split("?", 1)[0] in POLL_PATHS:
        return None
    record = {key: fields[key] for key in REQUEST_FIELDS if key in fields}
    if show_clients:
        for key in CLIENT_FIELDS:
            if key in fields:
                record["client"] = fields[key]
                break
    key = _KEY_FIELD.search(entry["message"])  # raw text: names like "007" stay strings
    if key:
        record["key"] = key[1]
    record["t"] = entry["t"]
    return record


# --------------------------------------------------------------------------- rates

class CounterRates:
    """Deltas of Gufo's counters between polls, re-baselined on restart."""

    def __init__(self):
        self.previous: dict[str, float] | None = None
        self.previous_t = 0.0

    def update(self, metrics: dict[str, float], t: float) -> dict[str, float] | None:
        current = {key: metrics[name] for key, name in COUNTERS.items() if name in metrics}
        previous, previous_t = self.previous, self.previous_t
        self.previous, self.previous_t = current, t
        if previous is None or t <= previous_t:
            return None
        if any(current.get(key, 0.0) < value for key, value in previous.items()):
            return None  # counters dropped: Gufo restarted, this poll is the new baseline
        return {key: current[key] - previous.get(key, 0.0) for key in current}

    def reset(self):
        self.previous = None


class SlotRates:
    """Live decode and prefill rates per session from successive /slots polls.

    Gufo adds a request to its counters only when it completes, so live rates
    come from each slot's n_decoded and n_prompt_tokens_processed. A single
    poll interval is noisy (tokens land in speculative bursts), so the decode
    rate spans the task's samples of the last RATE_WINDOW_S seconds since its
    first generated token; a new task starts a new window.
    """

    def __init__(self, window_s: float = RATE_WINDOW_S):
        self.window_s = window_s
        self.previous: dict = {}  # key -> (task or None, t, samples of (t, decoded, processed))

    def update(self, slots: list[dict], t: float) -> dict:
        rates = {}
        seen = set()
        for slot in slots:
            key = (slot.get("model"), slot.get("id"))
            seen.add(key)
            before = self.previous.get(key)
            if not slot.get("is_processing") or (before is not None and t <= before[1]):
                self.previous[key] = (None, t, collections.deque())
                rates[key] = (0.0, 0.0)
                continue
            task = slot.get("id_task")
            if before is not None and before[0] == task:
                samples = before[2]
            else:
                # New task: it started after the previous poll, with nothing done then.
                samples = collections.deque()
                if before is not None:
                    samples.append((before[1], 0, 0))
            samples.append((t, _decoded(slot), int(slot.get("n_prompt_tokens_processed") or 0)))
            while len(samples) > 2 and samples[1][0] <= t - self.window_s:
                samples.popleft()
            self.previous[key] = (task, t, samples)
            rates[key] = (_decode_rate(samples), _prefill_rate(samples))
        for key in list(self.previous):
            if key not in seen:
                del self.previous[key]
        return rates

    def reset(self):
        self.previous.clear()


def _decode_rate(samples) -> float | None:
    """Tokens per second since the first generated token, or None while that span is
    shorter than MIN_RATE_SPAN_S: one poll interval holds a partial first interval and
    speculative bursts, so an early reading would be off by up to half."""
    generating = [s for s in samples if s[1] > 0]
    if len(generating) < 2 or generating[-1][0] - generating[0][0] < MIN_RATE_SPAN_S:
        return None
    first, last = generating[0], generating[-1]
    return max(0, last[1] - first[1]) / (last[0] - first[0])


def _prefill_rate(samples) -> float:
    if len(samples) < 2 or samples[-1][2] <= samples[-2][2]:
        return 0.0  # prefill finished or not started
    reading = [s for s in samples if s[1] == 0]  # before the first generated token
    first = reading[0] if reading else samples[-2]
    last = samples[-1]
    return max(0, last[2] - first[2]) / (last[0] - first[0])


def _decoded(slot: dict) -> int:
    tokens = slot.get("next_token")
    if isinstance(tokens, list) and tokens and isinstance(tokens[0], dict):
        return int(tokens[0].get("n_decoded") or 0)
    return 0


# --------------------------------------------------------------------------- sources

class GufoClient:
    def __init__(self, base_url: str, api_key: str | None, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self._headers = {"Accept": "*/*", "User-Agent": "gufo-monitor"}
        if api_key:
            self._headers["Authorization"] = "Bearer " + api_key
        self.timeout = timeout

    def get(self, path: str) -> tuple[int, bytes]:
        """GET path; returns (status, body). Raises OSError when unreachable."""
        request = urllib.request.Request(self.base_url + path, headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(4 << 20)
        except urllib.error.HTTPError as error:
            try:
                body = error.read(64 << 10)
            finally:
                error.close()
            return error.code, body

    def get_json(self, path: str):
        status, body = self.get(path)
        if status != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_number(path: Path | None) -> float | None:
    if path is None:
        return None
    text = _read_text(path)
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


class Telemetry:
    """GPU (amdgpu sysfs) and host (/proc) readings; no root needed."""

    def __init__(self, card: str = "auto", proc: Path = Path("/proc")):
        self.device = self._find_device(card)
        self.hwmon = self._find_hwmon(self.device)
        self.proc = proc
        self._cpu_previous: tuple[int, int] | None = None

    @staticmethod
    def _find_device(card: str) -> Path | None:
        root = Path("/sys/class/drm")
        names = [card] if card != "auto" else sorted(
            p.name for p in root.glob("card*") if re.fullmatch(r"card\d+", p.name))
        for name in names:
            device = root / name / "device"
            if (device / "gpu_busy_percent").exists() and (
                    card != "auto" or _read_text(device / "vendor") == "0x1002"):
                return device
        return None

    @staticmethod
    def _find_hwmon(device: Path | None) -> Path | None:
        if device is None:
            return None
        for hwmon in sorted((device / "hwmon").glob("hwmon*")):
            if _read_text(hwmon / "name") == "amdgpu":
                return hwmon
        return None

    def read(self) -> dict:
        out: dict[str, float | None] = {}
        device, hwmon = self.device, self.hwmon
        out["gpu_busy_pct"] = _read_number(device / "gpu_busy_percent") if device else None
        used = _read_number(device / "mem_info_gtt_used") if device else None
        total = _read_number(device / "mem_info_gtt_total") if device else None
        out["gtt_used_gb"] = used / 1e9 if used is not None else None
        out["gtt_total_gb"] = total / 1e9 if total is not None else None
        temp = _read_number(hwmon / "temp1_input") if hwmon else None
        out["gpu_temp_c"] = temp / 1000 if temp is not None else None
        power = None
        if hwmon:
            power = _read_number(hwmon / "power1_average")
            if power is None:
                power = _read_number(hwmon / "power1_input")
        out["gpu_power_w"] = power / 1e6 if power is not None else None
        freq = _read_number(hwmon / "freq1_input") if hwmon else None
        out["gpu_mhz"] = freq / 1e6 if freq is not None else None
        out.update(self._memory())
        out["cpu_pct"] = self._cpu()
        return out

    def _memory(self) -> dict:
        values = {}
        try:
            with open(self.proc / "meminfo") as meminfo:
                for line in meminfo:
                    key, _, rest = line.partition(":")
                    if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                        values[key] = int(rest.split()[0]) * 1024
        except (OSError, ValueError, IndexError):
            return {"mem_avail_gb": None, "mem_total_gb": None, "swap_used_gb": None,
                    "swap_total_gb": None}
        swap_used = None
        if "SwapTotal" in values and "SwapFree" in values:
            swap_used = (values["SwapTotal"] - values["SwapFree"]) / 1e9
        return {
            "mem_avail_gb": values["MemAvailable"] / 1e9 if "MemAvailable" in values else None,
            "mem_total_gb": values["MemTotal"] / 1e9 if "MemTotal" in values else None,
            "swap_used_gb": swap_used,
            "swap_total_gb": values["SwapTotal"] / 1e9 if "SwapTotal" in values else None,
        }

    def _cpu(self) -> float | None:
        try:
            with open(self.proc / "stat") as stat:
                fields = [int(x) for x in stat.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields[:8])
        previous, self._cpu_previous = self._cpu_previous, (idle, total)
        if previous is None or total <= previous[1]:
            return None
        return 100.0 * (1 - (idle - previous[0]) / (total - previous[1]))


# --------------------------------------------------------------------------- state

class Monitor:
    """Shared state: polled samples, ring buffers, recent requests, SSE fan-out."""

    def __init__(self, gufo: GufoClient, telemetry: Telemetry, poll_s: float = 1.0,
                 history_s: float = 900.0, info_every_s: float = 30.0):
        self.gufo = gufo
        self.telemetry = telemetry
        self.poll_s = poll_s
        self.info_every_s = info_every_s
        self.cond = threading.Condition()
        self.seq = 0
        size = max(2, int(history_s / poll_s))
        self.times: collections.deque = collections.deque(maxlen=size)
        self.series = {key: collections.deque(maxlen=size) for key in SERIES}
        self.window: collections.deque = collections.deque()  # (t, counter deltas)
        self.requests: collections.deque = collections.deque(maxlen=MAX_REQUESTS)
        self.request_seq = 0
        self.log_totals = {"requests": 0, "errors": 0}
        self.key_usage: dict[str, dict] = {}  # per --api-key-file key, from the log
        self.key_reload: dict | None = None
        self.counter_rates = CounterRates()
        self.slot_rates = SlotRates()
        self.status = {"state": "down", "detail": "not polled yet", "model": None}
        self.models: list[dict] = []
        self.props: dict = {}
        self.info_t = -math.inf
        self.slots: list[dict] = []
        self.sample: dict = {}
        self.totals: dict = {}
        self.started = time.time()
        self.tick_payload = b"{}"
        self._published_request_seq = 0
        self.billing = None  # with --billing-config: meters each completed request
        self.stats: StatsStore | None = None  # hourly sums for the long-window tile

    # -- polling

    def poll_once(self, t: float | None = None):
        live = t is None
        t = time.time() if live else t
        status = self._poll_status()
        metrics: dict[str, float] = {}
        slots: list[dict] = []
        slots_t = t
        if status["state"] == "ready":
            if t - self.info_t >= self.info_every_s or self.status["state"] != "ready":
                self._poll_info()
                self.info_t = t
            slots = self._poll_slots()
            # Rates divide by the time between /slots answers, not between poll starts.
            slots_t = time.time() if live else t
            metrics = self._poll_metrics()
        else:
            self.counter_rates.reset()
            self.slot_rates.reset()
        deltas = self.counter_rates.update(metrics, t) if metrics else None
        rates = self.slot_rates.update(slots, slots_t)
        telemetry = self.telemetry.read()
        with self.cond:
            self._record(t, status, metrics, deltas, slots, rates, telemetry)
            self._publish()
        if self.stats is not None:
            self.stats.maybe_flush(t)

    def _poll_status(self) -> dict:
        try:
            status, body = self.gufo.get("/ready")
        except (OSError, ValueError) as error:
            return {"state": "down", "detail": _short_error(error), "model": None}
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if status == 200:
            return {"state": "ready", "detail": "", "model": data.get("model")}
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = error.get("code") or data.get("status")
        if status == 503 and code == "loading":
            return {"state": "loading", "detail": "loading model", "model": None}
        if status == 503 and code == "not_ready":
            return {"state": "loading", "detail": "model not ready", "model": None}
        if code == "device_lost":
            return {"state": "down", "detail": "GPU device lost", "model": None}
        if status == 401:
            return {"state": "down", "detail": "Gufo rejected the monitor's API key", "model": None}
        return {"state": "down", "detail": f"HTTP {status}", "model": None}

    def _poll_info(self):
        try:
            listing = self.gufo.get_json("/v1/models")
        except OSError:
            return
        models = []
        if isinstance(listing, dict) and isinstance(listing.get("data"), list):
            for item in listing["data"]:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    models.append({"id": item["id"], "context_length": item.get("context_length")})
        props = {}
        targets = [m["id"] for m in models] if len(models) > 1 else [None]
        for model in targets:
            path = "/props" + ("?model=" + urllib.parse.quote(model, safe="") if model else "")
            try:
                data = self.gufo.get_json(path)
            except OSError:
                continue
            if not isinstance(data, dict):
                continue
            settings = data.get("default_generation_settings")
            props[model or (models[0]["id"] if models else "")] = {
                "alias": data.get("model_alias"),
                "build": data.get("build_info"),
                "total_slots": data.get("total_slots"),
                "n_ctx": settings.get("n_ctx") if isinstance(settings, dict) else None,
            }
        with self.cond:
            self.models, self.props = models, props

    def _poll_metrics(self) -> dict[str, float]:
        try:
            status, body = self.gufo.get("/metrics")
        except OSError:
            return {}
        return parse_prometheus(body.decode("utf-8", "replace")) if status == 200 else {}

    def _poll_slots(self) -> list[dict]:
        try:
            data = self.gufo.get_json("/slots")
        except OSError:
            return []
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    def _record(self, t, status, metrics, deltas, slots, rates, telemetry):
        self.status = status
        if status["state"] != "ready":
            self.slots = []
        else:
            self.slots = [_slot_view(slot, rates.get((slot.get("model"), slot.get("id")),
                                                     (0.0, 0.0))) for slot in slots]
        # Sessions in their first MIN_RATE_SPAN_S of generation have no rate yet.
        live = [s for s in self.slots if s["processing"]]
        known = [s["decode_tps"] for s in live if s["decode_tps"] is not None]
        generating = any(s["decoded_tokens"] > 0 for s in live)
        decode = sum(known) if known or not generating else None
        prefill = sum(s["prefill_tps"] for s in self.slots)

        if deltas is not None:
            self.window.append((t, deltas))
        while self.window and self.window[0][0] < t - CARD_WINDOW_S:
            self.window.popleft()

        def ratio(numerator, *denominator, source=None):
            source = deltas if source is None else source
            if not source:
                return None
            bottom = sum(source.get(k, 0.0) for k in denominator)
            return 100.0 * source.get(numerator, 0.0) / bottom if bottom > 0 else None

        windowed = collections.Counter()
        for _, d in self.window:
            windowed.update(d)
        sample = {
            "t": t,
            "decode_tps": decode,
            "prefill_tps": prefill,
            "running": metrics.get("llamacpp:requests_processing"),
            "queued": metrics.get("llamacpp:requests_deferred"),
            "kv_usage_pct": _pct(metrics.get("llamacpp:kv_cache_usage_ratio")),
            "cache_reuse_pct": ratio("cached", "cached", "prompt"),
            "accept_pct": ratio("accepted", "draft"),
            "cache_reuse_window_pct": ratio("cached", "cached", "prompt", source=windowed),
            "accept_window_pct": ratio("accepted", "draft", source=windowed),
            **telemetry,
        }
        self.sample = sample
        if self.stats is not None:
            self.stats.add_sample(t, sample)
        self.times.append(round(t, 3))
        for key in SERIES:
            value = sample.get(key)
            self.series[key].append(None if value is None else round(value, 3))
        if metrics:
            counters = {key: metrics.get(name) for key, name in COUNTERS.items()}
            self.totals = {
                "prompt_tokens": counters["prompt"],
                "cached_tokens": counters["cached"],
                "generated_tokens": counters["predicted"],
                "device_lost": counters["device_lost"],
                "cache_reuse_pct": ratio("cached", "cached", "prompt", source={
                    k: v or 0.0 for k, v in counters.items()}),
                "accept_pct": ratio("accepted", "draft", source={
                    k: v or 0.0 for k, v in counters.items()}),
            }

    # -- log

    def add_request(self, record: dict):
        with self.cond:
            self.request_seq += 1
            record["seq"] = self.request_seq
            self.requests.append(record)
            self.log_totals["requests"] += 1
            status = record.get("status")
            failed = isinstance(status, int) and status >= 400
            if failed:
                self.log_totals["errors"] += 1
            key = record.get("key")
            if isinstance(key, str):
                usage = self.key_usage.setdefault(key, {
                    "requests": 0, "errors": 0, "prompt_tokens": 0, "cached_tokens": 0,
                    "generated_tokens": 0, "last_t": None})
                usage["requests"] += 1
                usage["errors"] += failed
                for field in ("prompt_tokens", "cached_tokens", "generated_tokens"):
                    value = record.get(field)
                    if isinstance(value, int):
                        usage[field] += value
                usage["last_t"] = max(usage["last_t"] or 0, record["t"] or 0) or None
        if self.billing is not None:
            self.billing.on_request(record)
        if self.stats is not None:
            self.stats.add_request(record)

    def set_key_reload(self, status: dict):
        with self.cond:
            if self.key_reload is None or (status["t"] or 0) >= (self.key_reload["t"] or 0):
                self.key_reload = status

    # -- views

    def _live(self) -> dict:
        return {
            "status": self.status,
            "models": self.models,
            "props": self.props,
            "slots": self.slots,
            "sample": self.sample,
            "totals": self.totals,
            "log_totals": self.log_totals,
            "key_usage": self.key_usage,
            "key_reload": self.key_reload,
        }

    def _publish(self):
        live = self._live()
        live["seq"] = self.seq + 1
        live["last_request_seq"] = self.request_seq
        live["new_requests"] = [r for r in list(self.requests)[-50:]
                                if r["seq"] > self._published_request_seq]
        self._published_request_seq = self.request_seq
        self.tick_payload = json.dumps(live, separators=(",", ":"), allow_nan=False).encode()
        self.seq += 1
        self.cond.notify_all()

    def snapshot(self) -> bytes:
        with self.cond:
            state = self._live()
            state.update({
                "seq": self.seq,
                "poll_s": self.poll_s,
                "history_len": self.times.maxlen,
                "monitor_started": self.started,
                "history": {"t": list(self.times),
                            **{key: list(values) for key, values in self.series.items()}},
                "requests": list(self.requests),
                "last_request_seq": self.request_seq,
            })
            return json.dumps(state, separators=(",", ":"), allow_nan=False).encode()

    def wait_tick(self, after_seq: int, timeout: float) -> tuple[int, bytes | None]:
        with self.cond:
            if self.seq <= after_seq:
                self.cond.wait(timeout)
            if self.seq <= after_seq:
                return after_seq, None
            return self.seq, self.tick_payload

    def run(self, stop: threading.Event):
        next_t = time.monotonic()
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as error:  # keep polling; one bad response must not stop the monitor
                print(f"gufo-monitor: poll failed: {_short_error(error)}", file=sys.stderr)
            next_t += self.poll_s
            delay = next_t - time.monotonic()
            if delay < 0:
                next_t = time.monotonic()
                delay = 0
            stop.wait(delay)


def _pct(value):
    return None if value is None else 100.0 * value


def _short_error(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    return str(reason if reason is not None else error)[:200] or type(error).__name__


def _slot_view(slot: dict, rates: tuple[float, float]) -> dict:
    processing = bool(slot.get("is_processing"))
    tokens = slot.get("next_token")
    first = tokens[0] if isinstance(tokens, list) and tokens and isinstance(tokens[0], dict) \
        else {}
    return {
        "id": slot.get("id"),
        "model": slot.get("model"),
        "n_ctx": slot.get("n_ctx"),
        "processing": processing,
        "speculative": bool(slot.get("speculative")),
        "prompt_tokens": int(slot.get("n_prompt_tokens") or 0),
        "cached_tokens": int(slot.get("n_prompt_tokens_cache") or 0),
        "processed_tokens": int(slot.get("n_prompt_tokens_processed") or 0),
        "decoded_tokens": _decoded(slot),
        "remaining": first.get("n_remain"),
        "decode_tps": rates[0] if processing else 0.0,
        "prefill_tps": rates[1] if processing else 0.0,
    }


# --------------------------------------------------------------------------- log tail

class LogTail:
    """Follows Gufo's log (podman container or file) and feeds completed requests."""

    def __init__(self, source: str, monitor: Monitor, show_clients: bool, log_utc: bool,
                 since: str = "1h"):
        kind, _, target = source.partition(":")
        if kind not in ("podman", "file") or not target:
            raise ValueError(f"unsupported log source {source!r}")
        self.kind, self.target = kind, target
        self.monitor = monitor
        self.show_clients = show_clients
        self.log_utc = log_utc
        self.since = since
        self._seen: set[str] = set()
        self._seen_order: collections.deque = collections.deque()

    def feed(self, line: str):
        entry = parse_log_line(line, self.log_utc)
        if entry is None:
            return
        if self.monitor.stats is not None:
            self.monitor.stats.note_log_time(entry["t"])
        reload = key_reload_status(entry)
        if reload is not None:
            self.monitor.set_key_reload(reload)
            return
        record = request_record(entry, self.show_clients)
        if record is None:
            return
        # A restarted follower replays its window; drop lines already counted.
        digest = hashlib.blake2b(line.encode("utf-8", "replace"), digest_size=12).hexdigest()
        if digest in self._seen:
            return
        self._seen.add(digest)
        self._seen_order.append(digest)
        if len(self._seen_order) > 4 * MAX_REQUESTS:
            self._seen.discard(self._seen_order.popleft())
        self.monitor.add_request(record)

    def run(self, stop: threading.Event):
        while not stop.is_set():
            try:
                if self.kind == "podman":
                    self._follow_podman(stop)
                else:
                    self._follow_file(stop)
            except Exception as error:
                print(f"gufo-monitor: log tail: {_short_error(error)}", file=sys.stderr)
            stop.wait(5.0)

    def _follow_podman(self, stop: threading.Event):
        command = ["podman", "logs", "--follow", "--timestamps", "--since", self.since, self.target]
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, text=True, errors="replace",
                              bufsize=1) as process:

            def watch():  # stop the follower on shutdown; exit with it otherwise
                while process.poll() is None:
                    if stop.wait(1.0):
                        process.terminate()
                        return

            threading.Thread(target=watch, daemon=True).start()
            assert process.stdout is not None
            for line in process.stdout:
                self.feed(line)

    def _follow_file(self, stop: threading.Event):
        path = Path(self.target)
        while not stop.is_set() and not path.exists():
            stop.wait(2.0)
        with open(path, "r", errors="replace") as handle:
            inode = os.fstat(handle.fileno()).st_ino
            size = os.fstat(handle.fileno()).st_size
            if size > 32 << 20:
                handle.seek(size - (32 << 20))
                handle.readline()
            pending = ""
            while not stop.is_set():
                chunk = handle.read(1 << 16)
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        self.feed(line)
                    continue
                try:
                    current = os.stat(path)
                except FileNotFoundError:
                    return
                if current.st_ino != inode or current.st_size < handle.tell():
                    return  # rotated or truncated: reopen from the start
                stop.wait(0.5)


# --------------------------------------------------------------------------- long-window stats

# The long-window tile's windows in hours: the current UTC hour and the N - 1 before it.
LONG_WINDOWS = {"24h": 24, "168h": 168}
STATS_RETENTION_S = 8 * 24 * 3600  # hourly buckets and request keys are kept this long
STATS_FLUSH_S = 60.0  # the poll thread writes pending sums this often
STATS_FLUSH_KEYS = 1000  # ... or once this many requests are pending (a backfill)
# Per-hour sums, the columns of the `hours` table. Columns added later reach existing
# files through StatsStore's migration and are 0 in older rows.
STATS_SUMS = (
    "requests", "errors", "prompt_tokens", "cached_tokens", "generated_tokens", "duration_ms",
    "decode_tokens", "decode_seconds", "prefill_tokens", "prefill_seconds",
    "samples", "sample_seconds", "active_samples", "gpu_busy_n", "gpu_busy_sum",
    "gpu_power_n", "gpu_power_sum", "gpu_temp_n", "gpu_temp_sum",
    # Requests with images; encode_requests are those of them that reported encode_ms.
    "image_requests", "images", "image_tokens", "encoder_images", "encode_ms",
    "image_tokens_reused", "encode_requests",
)
_IMAGE_SUMS = ("images", "image_tokens", "encoder_images", "encode_ms", "image_tokens_reused")
STATS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS hours (hour INTEGER PRIMARY KEY, "
    + ", ".join(f"{name} REAL NOT NULL DEFAULT 0" for name in STATS_SUMS) + ");\n"
    "CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, t REAL NOT NULL) WITHOUT ROWID;\n"
    "CREATE INDEX IF NOT EXISTS seen_t ON seen (t);\n"
    "CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value REAL) WITHOUT ROWID;\n"
)
_GPU_SAMPLES = (("gpu_busy", "gpu_busy_pct"), ("gpu_power", "gpu_power_w"),
                ("gpu_temp", "gpu_temp_c"))


def _count(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def request_sums(record: dict) -> dict | None:
    """The hourly sums one completed request adds; None unless it is inference (POST /v1/)."""
    path = record.get("path")
    if record.get("method") != "POST" or not isinstance(path, str) or not path.startswith("/v1/"):
        return None
    status = record.get("status")
    sums = {"requests": 1, "errors": int(isinstance(status, int) and status >= 400)}
    for field in ("prompt_tokens", "cached_tokens", "generated_tokens", "duration_ms"):
        if _count(record.get(field)):
            sums[field] = record[field]
    # Rates are weighed by tokens: a request decoded (prefilled) for its tokens over its
    # rate, so a window's rate is its tokens over that time, not a mean of request rates.
    for tokens, rate, kind in (("generated_tokens", "decode_tps", "decode"),
                               ("prefill_tokens", "prefill_tps", "prefill")):
        count, tps = record.get(tokens), record.get(rate)
        if _count(count) and count > 0 and _count(tps) and tps > 0:
            sums[kind + "_tokens"] = count
            sums[kind + "_seconds"] = count / tps
    images = record.get("images")
    if _count(images) and images > 0:
        sums["image_requests"] = 1
        for field in _IMAGE_SUMS:
            if _count(record.get(field)):
                sums[field] = record[field]
        if _count(record.get("encode_ms")):
            sums["encode_requests"] = 1
    return sums


class StatsStore:
    """Hourly (UTC) sums for the long-window tile in a SQLite file of their own (not
    billing's): completed inference requests from Gufo's log, and the 1 s GPU samples of
    the poll, which only the live monitor sees. A request counts once by `request@t`,
    so log replays, restarts and the start-up backfill never count it twice. Sums gather
    in memory and are written every STATS_FLUSH_S; reads add the pending ones."""

    def __init__(self, path: Path | str, poll_s: float = 1.0):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
        self.path = path
        self.poll_s = poll_s
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None,
                                    timeout=30.0)
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(STATS_SCHEMA)
            # Files from before a column existed gain it here; their rows read 0.
            present = {row[1] for row in self.conn.execute("PRAGMA table_info(hours)")}
            for name in STATS_SUMS:
                if name not in present:
                    self.conn.execute(
                        f"ALTER TABLE hours ADD COLUMN {name} REAL NOT NULL DEFAULT 0")
            meta = dict(self.conn.execute("SELECT name, value FROM meta").fetchall())
        self.log_since: float | None = meta.get("log_since")  # first log line seen
        self.samples_since: float | None = meta.get("samples_since")  # first GPU sample
        self._hours: dict[int, collections.Counter] = {}
        self._keys: dict[str, float] = {}
        self._flushed_t: float | None = None

    def _bucket(self, t: float) -> collections.Counter:
        return self._hours.setdefault(int(t // 3600), collections.Counter())

    def note_log_time(self, t: float | None):
        """The log covers time T: windows that start earlier have data only from its
        first line on."""
        if t is not None and (self.log_since is None or t < self.log_since):
            with self.lock:
                if self.log_since is None or t < self.log_since:
                    self.log_since = t

    def add_request(self, record: dict, now: float | None = None) -> bool:
        """Count a completed request (request_record's fields) once; whether it was new."""
        sums = request_sums(record)
        t = record.get("t")
        if sums is None or not _count(t):
            return False
        now = time.time() if now is None else now
        if t < now - STATS_RETENTION_S:
            return False
        request = record.get("request")
        key = f"{request}@{t:.3f}" if request is not None else "@{:.3f}/{}/{}/{}".format(
            t, record.get("duration_ms"), record.get("prompt_tokens"),
            record.get("generated_tokens"))
        self.note_log_time(t)
        with self.lock:
            if key in self._keys or self.conn.execute(
                    "SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone():
                return False
            self._keys[key] = t
            self._bucket(t).update(sums)
            if len(self._keys) >= STATS_FLUSH_KEYS:
                self._flush(now)
        return True

    def add_sample(self, t: float, sample: dict):
        """One poll: GPU readings, and whether a request was running or queued."""
        active = (sample.get("running") or 0) + (sample.get("queued") or 0) > 0
        with self.lock:
            bucket = self._bucket(t)
            bucket["samples"] += 1
            bucket["sample_seconds"] += self.poll_s
            bucket["active_samples"] += int(active)
            for name, key in _GPU_SAMPLES:
                value = sample.get(key)
                if value is not None:
                    bucket[name + "_n"] += 1
                    bucket[name + "_sum"] += value
            if self.samples_since is None or t < self.samples_since:
                self.samples_since = t

    def maybe_flush(self, now: float):
        if self._flushed_t is None:
            self._flushed_t = now
        elif now - self._flushed_t >= STATS_FLUSH_S:
            self.flush(now)

    def flush(self, now: float | None = None):
        with self.lock:
            self._flush(time.time() if now is None else now)

    def _flush(self, now: float):
        """Write the pending sums and keys and drop what is older than the retention."""
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            for hour, sums in self._hours.items():
                names = [name for name in STATS_SUMS if sums.get(name)]
                if names:
                    conn.execute(
                        f"INSERT INTO hours (hour, {', '.join(names)}) "
                        f"VALUES (?{', ?' * len(names)}) ON CONFLICT (hour) DO UPDATE SET "
                        + ", ".join(f"{name} = {name} + excluded.{name}" for name in names),
                        (hour, *(sums[name] for name in names)))
            conn.executemany("INSERT OR IGNORE INTO seen (key, t) VALUES (?, ?)",
                             self._keys.items())
            for name in ("log_since", "samples_since"):
                value = getattr(self, name)
                if value is not None:
                    conn.execute("INSERT INTO meta (name, value) VALUES (?, ?) ON CONFLICT (name) "
                                 "DO UPDATE SET value = min(value, excluded.value)", (name, value))
            oldest = now - STATS_RETENTION_S
            conn.execute("DELETE FROM hours WHERE hour < ?", (int(oldest // 3600),))
            conn.execute("DELETE FROM seen WHERE t < ?", (oldest,))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        self._hours.clear()
        self._keys.clear()
        self._flushed_t = now

    def needs_backfill(self, now: float | None = None) -> bool:
        """Whether the log data seen so far starts after the longest window does."""
        now = time.time() if now is None else now
        return self.log_since is None or self.log_since > now - max(LONG_WINDOWS.values()) * 3600

    def long_window(self, now: float | None = None) -> dict:
        """GET /api/stats/long: every window of LONG_WINDOWS from at most 168 hourly rows."""
        now = time.time() if now is None else now
        current = int(now // 3600)
        first = current - max(LONG_WINDOWS.values()) + 1
        with self.lock:
            hours = {row[0]: dict(zip(STATS_SUMS, row[1:])) for row in self.conn.execute(
                f"SELECT hour, {', '.join(STATS_SUMS)} FROM hours WHERE hour >= ?", (first,))}
            for hour, sums in self._hours.items():
                if hour >= first:
                    merged = hours.setdefault(hour, dict.fromkeys(STATS_SUMS, 0.0))
                    for name, value in sums.items():
                        merged[name] += value
            log_since, samples_since = self.log_since, self.samples_since
        return {
            "now": now, "since": log_since, "gpu_since": samples_since,
            "windows": {name: _stats_window(hours, current - n + 1, n, now, log_since,
                                            samples_since)
                        for name, n in LONG_WINDOWS.items()},
        }


def _stats_window(hours: dict, start_hour: int, window_hours: int, now: float,
                  log_since: float | None, samples_since: float | None) -> dict:
    total = dict.fromkeys(STATS_SUMS, 0.0)
    for hour, sums in hours.items():
        if hour >= start_hour:
            for name in STATS_SUMS:
                total[name] += sums[name]
    start = start_hour * 3600
    data_since = None if log_since is None else max(start, log_since)
    data_hours = 0.0 if data_since is None else max(0.0, now - data_since) / 3600

    def ratio(top, bottom, scale=1.0):
        return scale * total[top] / total[bottom] if total[bottom] > 0 else None

    return {
        "window_hours": window_hours,
        "start": start,
        "data_since": data_since,  # log data, so requests and tokens, from here on
        "data_hours": data_hours,
        "requests": int(total["requests"]),
        "errors": int(total["errors"]),
        "prompt_tokens": int(total["prompt_tokens"]),
        "cached_tokens": int(total["cached_tokens"]),
        "generated_tokens": int(total["generated_tokens"]),
        "decode_tps": ratio("decode_tokens", "decode_seconds"),
        "prefill_tps": ratio("prefill_tokens", "prefill_seconds"),
        "tokens_per_hour": total["generated_tokens"] / data_hours if data_hours > 0 else None,
        "cache_reuse_pct": ratio("cached_tokens", "prompt_tokens", 100.0),
        "serving_pct": ratio("active_samples", "samples", 100.0),
        "gpu_busy_pct": ratio("gpu_busy_sum", "gpu_busy_n"),
        "gpu_power_w": ratio("gpu_power_sum", "gpu_power_n"),
        "gpu_temp_c": ratio("gpu_temp_sum", "gpu_temp_n"),
        "gpu_hours": total["sample_seconds"] / 3600,  # polled time; the log has no samples
        "gpu_since": None if samples_since is None else max(start, samples_since),
        # Requests with images (none from Gufo builds without the image fields).
        "image_requests": int(total["image_requests"]),
        **{name: int(total[name]) for name in _IMAGE_SUMS if name != "encode_ms"},
        "encode_ms": total["encode_ms"],
        "encode_ms_per_request": ratio("encode_ms", "encode_requests"),
    }


def backfill_stats(stats: StatsStore, path: Path, log_utc: bool,
                   stop: threading.Event | None = None) -> int:
    """Feed the completed requests of Gufo's log file and its rotated predecessor PATH.1,
    from their first lines, to STATS; returns how many were new. Only request_record's
    fields are kept, and the store's request keys make a replay count nothing twice."""
    added = 0
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            handle = open(candidate, "r", errors="replace")
        except FileNotFoundError:
            continue
        with handle:
            first = True
            for line in handle:
                if stop is not None and stop.is_set():
                    return added
                if not first and "event=completed" not in line:
                    continue
                entry = parse_log_line(line, log_utc)
                if entry is None:
                    continue
                if first:
                    stats.note_log_time(entry["t"])
                    first = False
                record = request_record(entry, show_clients=False)
                if record is not None and stats.add_request(record):
                    added += 1
    stats.flush()
    return added


# --------------------------------------------------------------------------- auth and HTTP

def load_or_create_token(path: Path) -> str:
    try:
        token = path.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(token + "\n")
    print(f"gufo-monitor: created admin token in {path}", file=sys.stderr)
    return token


def read_api_key(path: Path) -> str:
    key = path.read_text().strip()
    if not key:
        raise ValueError(f"{path} is empty")
    return key


# --------------------------------------------------------------------------- settings (.env)

_ENV_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")


def read_env_file(path: Path) -> dict[str, str]:
    """The KEY=VALUE lines of a .env file: `#` starts a comment (a whole line, or after
    whitespace in an unquoted value), a value may be in single or double quotes, and
    nothing is expanded or unescaped. A missing file reads as empty."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    values = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        key, value = match[1], match[2]
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            rest = value[end + 1:].strip() if end > 0 else None
            if rest is None or (rest and not rest.startswith("#")):
                raise ValueError(f"{path}:{number}: unbalanced quotes in the value of {key}")
            value = value[1:end]
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def load_environment(path: Path | None, environ=os.environ) -> dict[str, str]:
    """The ENV_KEYS settings that are set, from the process environment or else from the
    .env file PATH; an empty value counts as unset."""
    values = read_env_file(path) if path is not None else {}
    for key in values:
        if key not in ENV_KEYS:
            print(f"gufo-monitor: {path}: ignoring unknown setting {key}", file=sys.stderr)
    settings = {}
    for key in ENV_KEYS:
        value = (environ.get(key) or "").strip() or values.get(key, "").strip()
        if value:
            settings[key] = value
    return settings


def default_env_file() -> Path:
    """.env next to this script if there is one, else in the config directory."""
    local = HERE / ".env"
    if local.exists():
        return local
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "gufo-monitor" / ".env"


# --------------------------------------------------------------------------- key file

KEY_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
KEY_DIGEST = re.compile(r"sha256:([0-9a-fA-F]{64})")
KEY_MODEL = re.compile(r"[^\s,]+")


class KeyFileError(ValueError):
    pass


def parse_key_line(line: str) -> dict | None:
    """One line of Gufo's --api-key-file, with Gufo's rules; None for blanks/comments."""
    fields = line.split()
    if not fields or fields[0].startswith("#"):
        return None
    if len(fields) < 2:
        raise KeyFileError("expected NAME sha256:HEX [access=...] [models=...]")
    name, credential, *options = fields
    if not KEY_NAME.fullmatch(name):
        raise KeyFileError("key name must be 1-64 characters of A-Z a-z 0-9 . _ -")
    digest = KEY_DIGEST.fullmatch(credential)
    if not digest:
        raise KeyFileError("credential must be sha256: followed by 64 hex digits")
    key = {"name": name, "digest": digest[1].lower(), "access": "inference", "models": []}
    seen = set()
    for option in options:
        option_name, separator, value = option.partition("=")
        if not separator or option_name not in ("access", "models") or option_name in seen:
            raise KeyFileError(f"unknown or repeated option '{option}'")
        seen.add(option_name)
        if option_name == "access":
            if value not in ("inference", "monitor"):
                raise KeyFileError("access must be inference or monitor")
            key["access"] = value
        else:
            models = value.split(",")
            if not value or not all(models):
                raise KeyFileError("models must list served model names")
            key["models"] = models
    if key["access"] == "monitor" and key["models"]:
        raise KeyFileError("a monitor key cannot list models")
    return key


def format_key_line(key: dict) -> str:
    parts = [key["name"], "sha256:" + key["digest"]]
    if key["access"] == "monitor":
        parts.append("access=monitor")
    if key["models"]:
        parts.append("models=" + ",".join(key["models"]))
    return " ".join(parts)


class KeyStore:
    """Edits Gufo's --api-key-file in place: other lines and comments are kept,
    each write replaces the file atomically, and only digests are stored.
    Gufo re-reads the file within about a second."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> tuple[list[str], list[dict]]:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return [], []
        keys = []
        for index, line in enumerate(lines):
            try:
                key = parse_key_line(line)
            except KeyFileError as error:
                raise KeyFileError(f"{self.path}: line {index + 1}: {error}") from None
            if key is not None:
                key["line"] = index
                keys.append(key)
        return lines, keys

    def _write(self, lines: list[str]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = self.path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write("".join(line + "\n" for line in lines))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validated(name, access, models) -> dict:
        if not isinstance(name, str) or not isinstance(access, str) or not isinstance(
                models, list) or not all(isinstance(m, str) for m in models):
            raise KeyFileError("name, access and models are required")
        if not KEY_NAME.fullmatch(name):
            raise KeyFileError("key name must be 1-64 characters of A-Z a-z 0-9 . _ -")
        if access not in ("inference", "monitor"):
            raise KeyFileError("access must be inference or monitor")
        if any(not KEY_MODEL.fullmatch(model) for model in models):
            raise KeyFileError("model names cannot be empty or contain spaces or commas")
        key = {"name": name, "digest": "0" * 64, "access": access, "models": models}
        parse_key_line(format_key_line(key))  # Gufo's rules, from one place
        return key

    def list(self) -> list[dict]:
        with self._lock:
            _, keys = self._read()
        return [{"name": k["name"], "access": k["access"], "models": k["models"]} for k in keys]

    def create(self, name, access, models) -> str:
        key = self._validated(name, access, models)
        token = secrets.token_urlsafe(32)
        key["digest"] = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            lines, keys = self._read()
            if any(k["name"] == name for k in keys):
                raise KeyFileError(f"key '{name}' already exists")
            self._write([*lines, format_key_line(key)])
        return token

    def update(self, name, access, models):
        key = self._validated(name, access, models)
        with self._lock:
            lines, keys = self._read()
            current = next((k for k in keys if k["name"] == name), None)
            if current is None:
                raise LookupError(name)
            key["digest"] = current["digest"]
            lines[current["line"]] = format_key_line(key)
            self._write(lines)

    def delete(self, name):
        with self._lock:
            lines, keys = self._read()
            current = next((k for k in keys if k["name"] == name), None)
            if current is None:
                raise LookupError(name)
            if len(keys) == 1:
                # Gufo refuses a file without keys and would keep this one.
                raise KeyFileError("cannot remove the last key")
            del lines[current["line"]]
            self._write(lines)

    def sync_managed(self, wanted: dict[str, dict], managed) -> bool:
        """Make the keys whose names satisfy `managed` exactly `wanted`, {name: {"digest",
        "models"}} of inference keys, in one atomic write; other lines stay as they are.
        Returns whether the file changed."""
        desired = {}
        for name, spec in wanted.items():
            if not managed(name):
                raise KeyFileError(f"key '{name}' is not a managed name")
            key = self._validated(name, "inference", list(spec["models"]))
            if not re.fullmatch(r"[0-9a-f]{64}", spec["digest"]):
                raise KeyFileError(f"key '{name}': digest must be 64 lowercase hex digits")
            key["digest"] = spec["digest"]
            desired[name] = format_key_line(key)
        with self._lock:
            lines, keys = self._read()
            updated = list(lines)
            drop = set()
            present = set()
            for key in keys:
                if not managed(key["name"]):
                    continue
                present.add(key["name"])
                line = desired.get(key["name"])
                if line is None:
                    drop.add(key["line"])
                else:
                    updated[key["line"]] = line
            updated = [line for index, line in enumerate(updated) if index not in drop]
            added = [line for name, line in desired.items() if name not in present]
            updated += added
            if updated == lines:
                return False
            if len(keys) - len(drop) + len(added) == 0:
                # Gufo refuses a file without keys and would keep the old ones.
                raise KeyFileError("cannot remove the last key")
            self._write(updated)
            return True


class AdminAuth:
    """The admin token. Scripts send it as `X-Admin-Token: TOKEN` or `Authorization:
    Bearer TOKEN`; browsers send the session cookie from POST /api/admin/login, which holds
    its expiry time and an HMAC of that under the token."""

    def __init__(self, token: str):
        self._token = token.encode()

    def check_token(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._token)

    def _sign(self, expires: int) -> bytes:
        return hmac.new(self._token, b"gufo-monitor-admin-v2:%d" % expires,
                        hashlib.sha256).hexdigest().encode()

    def session(self, now: float | None = None) -> str:
        """A new session cookie value, valid for ADMIN_COOKIE_MAX_AGE."""
        expires = int(time.time() if now is None else now) + ADMIN_COOKIE_MAX_AGE
        return f"{expires}.{self._sign(expires).decode()}"

    def valid_until(self, headers, now: float | None = None) -> float | None:
        """Until when the request's admin credentials hold: forever for the token, the
        expiry of a valid session cookie, None without either."""
        if self.check_token(headers.get("X-Admin-Token", "").strip()):
            return math.inf
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer ") and self.check_token(authorization[7:].strip()):
            return math.inf
        now = time.time() if now is None else now
        for part in headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            expires, _, signature = value.partition(".")
            if (name == ADMIN_COOKIE_NAME and 0 < len(expires) <= 12 and expires.isascii()
                    and expires.isdigit() and now < int(expires)
                    and hmac.compare_digest(signature.encode(), self._sign(int(expires)))):
                return float(expires)
        return None


class RateLimiter:
    """Sign-in attempts per key, e.g. "login:" + client address, in a sliding window."""

    def __init__(self, limit: int = SIGNIN_LIMIT, window_s: float = SIGNIN_WINDOW_S):
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, collections.deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 10_000:  # forget idle addresses
                for stale in [k for k, v in self._hits.items()
                              if not v or v[-1] <= now - self.window_s]:
                    del self._hits[stale]
            hits = self._hits.setdefault(key, collections.deque())
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def is_loopback(host: str | None) -> bool:
    """Whether HOST (an address or host name, without port) names this host's loopback."""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return (getattr(address, "ipv4_mapped", None) or address).is_loopback


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, monitor: Monitor, admin: AdminAuth, secure_cookie: bool,
                 keys: KeyStore | None = None, billing=None):
        super().__init__(address, Handler)
        self.monitor = monitor
        self.admin = admin
        self.secure_cookie = secure_cookie
        self.keys = keys  # None: no --keys-file, the key routes answer 404
        # Sign-in attempts (admin, portal login and signup), budgeted per kind and client.
        self.limiter = RateLimiter()
        # None without --billing-config: the account and portal routes answer 404.
        self.billing = billing
        self.portal = None
        if billing is not None:
            from billing.portal import Portal  # the package load_billing put on sys.path
            self.portal = Portal(billing, secure_cookie, self.limiter)
        self.streams = threading.BoundedSemaphore(MAX_STREAMS)
        self.stop = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    server: Server
    server_version = "gufo-monitor"
    sys_version = ""

    # -- plumbing

    def log_message(self, format, *args):  # quiet; failures only, without query strings
        pass

    def log_request(self, code="-", size="-"):
        if isinstance(code, int) and code >= 400 and code != 401:
            path = self.path.split("?", 1)[0]
            print(f"gufo-monitor: {self.command} {path} -> {code}", file=sys.stderr)

    def _send(self, status: int, body: bytes, content_type: str,
              headers: tuple[tuple[str, str], ...] = (), cache: str = "no-store"):
        self.send_response(status)
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, body: bytes):
        self._send(status, body, "application/json")

    def _redirect(self, location: str, headers: tuple[tuple[str, str], ...] = ()):
        self._send(303, b"", "text/plain", (("Location", location), *headers))

    def client_ip(self) -> str:
        """The client's address for rate limits. A loopback peer is a local tunnel or
        proxy relaying someone: Cloudflare's CF-Connecting-IP names them, else the last
        X-Forwarded-For entry. Anyone else is keyed by their own address, whatever
        headers they send."""
        peer = self.client_address[0]
        if not is_loopback(peer):
            return peer
        connecting = self.headers.get("CF-Connecting-IP", "").strip()
        if connecting:
            return connecting
        forwarded = self.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[-1].strip() or peer

    def _admin_until(self) -> float | None:
        """Until when this request has admin access, or None: see AdminAuth.valid_until."""
        return self.server.admin.valid_until(self.headers)

    def _admin(self) -> bool:
        """Admin access, which every monitor route needs."""
        return self._admin_until() is not None

    def _secure(self) -> bool:
        return self.server.secure_cookie or self.headers.get("X-Forwarded-Proto", "") == "https"

    def _admin_cookie(self, value: str, max_age: int) -> tuple[str, str]:
        cookie = (f"{ADMIN_COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; HttpOnly; "
                  "SameSite=Strict")
        return ("Set-Cookie", cookie + ("; Secure" if self._secure() else ""))

    def _read_json(self, limit: int = 16 << 10):
        """The request's JSON object body, or None. JSON is required so a
        cross-site form cannot submit it without a CORS preflight."""
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _error(self, status: int, message: str):
        self._json(status, json.dumps({"error": message}).encode())

    # -- API keys (Gufo's --api-key-file); the admin's only, like everything below

    def _keys_route(self, path: str) -> str | None:
        """The key name of /api/keys/NAME, "" for /api/keys, None otherwise."""
        if path == "/api/keys":
            return ""
        if path.startswith("/api/keys/"):
            return urllib.parse.unquote(path[len("/api/keys/"):])
        return None

    def _keys_get(self):
        try:
            keys = self.server.keys.list()
        except KeyFileError as error:
            return self._error(500, str(error))
        self._json(200, json.dumps({"keys": keys}).encode())

    def _keys_change(self, method: str, name: str):
        store = self.server.keys
        try:
            if method == "DELETE" and name:
                store.delete(name)
                return self._send(204, b"", "text/plain")
            body = self._read_json()
            if body is None:
                return self._error(400, "expected a JSON object")
            access = body.get("access", "inference")
            models = body.get("models", [])
            if method == "POST" and not name:
                token = store.create(body.get("name"), access, models)
                payload = {"name": body["name"], "token": token}
                return self._json(201, json.dumps(payload).encode())
            if method == "PUT" and name:
                store.update(name, access, models)
                return self._send(204, b"", "text/plain")
        except LookupError:
            return self._error(404, f"no key named '{name}'")
        except KeyFileError as error:
            return self._error(400, str(error))
        self._error(405, "method not allowed")

    # -- admin sign-in, from the profile menu of either page

    def _admin_login(self):
        """The admin token for the admin session cookie, sent to /monitor and /api/.
        Attempts are rate limited per client address, like the portal's sign-in."""
        if not self.server.limiter.allow("admin:" + self.client_ip()):
            return self._error(429, "too many attempts, try again in a minute")
        body = self._read_json(4096)
        token = body.get("token") if body else None
        if not isinstance(token, str) or not self.server.admin.check_token(token.strip()):
            time.sleep(0.5)
            return self._error(401, "wrong admin token")
        self._send(204, b"", "text/plain", (
            self._admin_cookie(self.server.admin.session(), ADMIN_COOKIE_MAX_AGE),))

    def _admin_logout(self):
        self._send(204, b"", "text/plain", (self._admin_cookie("", 0),))

    def _admin_session(self):
        """What the pages show: the monitor link and the admin areas for "admin"."""
        self._json(200, json.dumps({"admin": self._admin()}).encode())

    def _portal(self, method: str):
        if self.server.portal is None:
            return self._json(404, json.dumps({
                "error": "not_found", "message": "the credits portal is not enabled"}).encode())
        self.server.portal.handle(self, method)

    # -- portal accounts (--billing-config)

    def _account_error(self, status: int, code: str, message: str):
        self._json(status, json.dumps({"error": code, "message": message}).encode())

    def _accounts_get(self):
        if self.server.billing is None:
            return self._account_error(404, "not_found", "billing is not configured")
        self._json(200, json.dumps(self.server.billing.admin_accounts()).encode())

    def _accounts_change(self, method: str, path: str):
        billing = self.server.billing
        if billing is None:
            return self._account_error(404, "not_found", "billing is not configured")
        match = re.fullmatch(r"/api/admin/accounts/(\d{1,18})/([a-z]+)", path)
        if match is None:
            return self._account_error(404, "not_found", "no such route")
        if method != "POST":
            return self._account_error(405, "method_not_allowed", "use POST")
        body = None
        if match[2] == "credit":
            body = self._read_json()
            if body is None:
                return self._account_error(400, "bad_request", "expected a JSON object")
        try:
            result = billing.admin_action(int(match[1]), match[2], body)
        except billing.Error as error:
            return self._account_error(error.status, error.code, error.message)
        self._json(200, json.dumps(result).encode())

    def _pricing(self, method: str):
        """GET: the price table and the models Gufo serves; PUT: replace the table."""
        billing = self.server.billing
        if billing is None:
            return self._account_error(404, "not_found", "billing is not configured")
        if method == "PUT":
            body = self._read_json(64 << 10)
            if body is None:
                return self._account_error(400, "bad_request", "expected a JSON object")
            try:
                table = billing.save_pricing(body)
            except billing.Error as error:
                return self._account_error(error.status, error.code, error.message)
            except OSError as error:
                print(f"gufo-monitor: saving the prices failed: {error}", file=sys.stderr)
                return self._account_error(500, "save_failed", "could not write the prices file")
        elif method == "GET":
            table = billing.pricing.to_dict()
        else:
            return self._account_error(405, "method_not_allowed", "use GET or PUT")
        served = [model["id"] for model in self.server.monitor.models]
        self._json(200, json.dumps({**table, "served_models": served}).encode())

    def _usage_get(self):
        """Internal (owner and hand-made keys) and portal users' usage of the last
        ?days=N UTC days (default 30)."""
        billing = self.server.billing
        if billing is None:
            return self._account_error(404, "not_found", "billing is not configured")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        values = query.get("days", ["30"])
        days = int(values[0]) if len(values) == 1 and re.fullmatch(r"\d{1,3}", values[0]) else 0
        try:
            report = billing.admin_usage(days)
        except billing.Error as error:
            return self._account_error(error.status, error.code, error.message)
        self._json(200, json.dumps(report).encode())

    def _query_int(self, name: str, default: int) -> int:
        """?NAME=N as an integer, DEFAULT when absent, 0 (refused later) when malformed."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        values = query.get(name, [str(default)])
        return int(values[0]) if len(values) == 1 and re.fullmatch(r"\d{1,4}", values[0]) else 0

    def _deposits(self, kind: str):
        """Credited deposits with the receiver's balances (?limit=N, default 200), the
        same as CSV, or a read-only re-check against the chain (?limit=N, at most 50)."""
        billing = self.server.billing
        if billing is None:
            return self._account_error(404, "not_found", "billing is not configured")
        try:
            if kind == "csv":
                return self._send(200, billing.deposits_csv().encode(), "text/csv; charset=utf-8", (
                    ("Content-Disposition", 'attachment; filename="gufo-deposits.csv"'),))
            if kind == "verify":
                report = billing.verify_deposits(self._query_int("limit", 50))
            else:
                report = billing.admin_deposits(self._query_int("limit", 200))
        except billing.Error as error:
            return self._account_error(error.status, error.code, error.message)
        self._json(200, json.dumps(report).encode())

    def _long_stats(self):
        """The last 24 h / 7 days tile: StatsStore.long_window, O(hours)."""
        stats = self.server.monitor.stats
        if stats is None:
            return self._json(404, b'{"error":"not_found"}')
        try:
            report = stats.long_window()
            billing = self.server.billing
            # {users, internal, total} micro-dollars per window; null without --billing-config.
            report["revenue_micro"] = None if billing is None else {
                name: billing.revenue_micro(hours, report["now"])
                for name, hours in LONG_WINDOWS.items()}
            body = json.dumps(report, allow_nan=False).encode()
        except sqlite3.Error as error:
            print(f"gufo-monitor: --stats-file: {error}", file=sys.stderr)
            return self._error(500, "stats unavailable")
        self._json(200, body)

    def _mutation(self, method: str):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/portal/"):
            return self._portal(method)
        if path in ("/api/admin/login", "/api/admin/logout"):
            if method != "POST":
                return self._error(405, "method not allowed")
            return self._admin_login() if path == "/api/admin/login" else self._admin_logout()
        if not self._admin():
            return self._error(401, "unauthorized")
        if path.startswith("/api/admin/accounts/"):
            return self._accounts_change(method, path)
        if path == "/api/admin/pricing":
            return self._pricing(method)
        name = self._keys_route(path)
        if name is None or self.server.keys is None:
            return self._error(404, "not_found")
        self._keys_change(method, name)

    def do_PUT(self):
        self._mutation("PUT")

    def do_DELETE(self):
        self._mutation("DELETE")

    # -- routes: / and /api/portal/* are public; /monitor and the rest of /api/ are the
    # admin's alone, apart from signing in and out and asking whether one is signed in

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path in ("/", "/index.html"):
            return self._file(PORTAL_STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(204, b"", "text/plain")
        if path.startswith("/api/portal/"):
            return self._portal("GET")
        if path == "/api/admin/session":
            return self._admin_session()
        until = self._admin_until()
        if until is None:
            if path == "/monitor":
                return self._redirect("/")  # the admin signs in from the profile menu
            if path.startswith("/api/"):
                return self._json(401, b'{"error":"unauthorized"}')
            return self._json(404, b'{"error":"not_found"}')
        if path == "/monitor":
            return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(200, self.server.monitor.snapshot())
        if path == "/api/stream":
            return self._stream(until)
        if path == "/api/stats/long":
            return self._long_stats()
        if path == "/api/admin/accounts":
            return self._accounts_get()
        if path == "/api/admin/pricing":
            return self._pricing("GET")
        if path == "/api/admin/usage":
            return self._usage_get()
        if path == "/api/admin/deposits":
            return self._deposits("list")
        if path == "/api/admin/deposits.csv":
            return self._deposits("csv")
        if path == "/api/admin/deposits/verify":
            return self._deposits("verify")
        if self._keys_route(path) == "" and self.server.keys is not None:
            return self._keys_get()
        self._json(404, b'{"error":"not_found"}')

    def do_POST(self):
        self._mutation("POST")

    def _file(self, path: Path, content_type: str, cache: str = "no-store"):
        try:
            body = path.read_bytes()
        except OSError:
            return self._json(404, b'{"error":"not_found"}')
        self._send(200, body, content_type, cache=cache)

    def _static(self, name: str):
        entry = STATIC_FILES.get(name)
        if entry is None:
            return self._json(404, b'{"error":"not_found"}')
        directory, content_type = entry
        self._file(directory / name, content_type, cache="no-cache")

    def _stream(self, until: float):
        """Ticks until the client leaves, the server stops or, at time UNTIL, the admin
        session cookie expires: the browser's reconnect then gets 401."""
        if not self.server.streams.acquire(blocking=False):
            return self._json(503, b'{"error":"too_many_streams"}')
        try:
            self.send_response(200)
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            monitor = self.server.monitor
            seq = monitor.seq
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not self.server.stop.is_set():
                left = until - time.time()
                if left <= 0:
                    break
                seq, payload = monitor.wait_tick(seq, min(15.0, left))
                if payload is None:
                    self.wfile.write(b": keep-alive\n\n")
                else:
                    self.wfile.write(b"event: tick\ndata: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.server.streams.release()


# --------------------------------------------------------------------------- main

def parse_args(argv=None):
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gufo", default="http://127.0.0.1:8080", help="Gufo base URL")
    parser.add_argument("--gufo-key-file", type=Path,
                        help="file holding Gufo's --api-key; read and used only by this process")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="listen address, for the monitor and the portal")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--poll", type=float, default=1.0, help="poll interval in seconds")
    parser.add_argument("--history", type=float, default=900.0, help="sparkline window, s")
    parser.add_argument("--log-source", default="none",
                        help="where Gufo logs: podman:CONTAINER, file:PATH or none")
    parser.add_argument("--log-since", default="1h",
                        help="podman log backlog to read at start")
    parser.add_argument("--log-utc", action="store_true",
                        help="file log timestamps are UTC (Gufo inside a container)")
    parser.add_argument("--gpu-card", default="auto", help="DRM card name, e.g. card1")
    parser.add_argument("--show-clients", action="store_true",
                        help="show client ids from the log")
    parser.add_argument("--secure-cookie", action="store_true",
                        help="always mark the sign-in cookies Secure (served over https)")
    parser.add_argument("--keys-file", type=Path,
                        help="Gufo's --api-key-file: view keys and usage, and manage them "
                             "with the admin token")
    parser.add_argument("--admin-token-file", type=Path,
                        default=config / "gufo-monitor" / "admin-token",
                        help="admin token, the only way into the monitor: browser sign-in, "
                             "scripts (X-Admin-Token or Authorization: Bearer), key and account "
                             "management; created (mode 0600) if missing")
    parser.add_argument("--billing-config", type=Path,
                        help="enable prepaid credits and the public portal (needs --keys-file)")
    parser.add_argument("--stats-file", type=Path,
                        default=config / "gufo-monitor" / "stats.sqlite",
                        help="hourly usage and GPU sums for the last 24 h / 7 days tile, kept "
                             "8 days; created (mode 0600) if missing")
    parser.add_argument("--env-file", type=Path,
                        help="settings such as GUFO_PUBLIC_URL (default: .env next to this "
                             "script if present, else $XDG_CONFIG_HOME/gufo-monitor/.env; "
                             "the process environment wins)")
    args = parser.parse_args(argv)
    if args.env_file is None:
        args.env_file = default_env_file()  # optional: a missing default file is fine
    elif not args.env_file.is_file():
        parser.error(f"--env-file {args.env_file}: no such file")
    if args.billing_config and not args.keys_file:
        parser.error("--billing-config needs --keys-file")
    if args.billing_config and args.log_source == "none":
        parser.error("--billing-config needs --log-source: usage is metered from Gufo's log")
    if args.poll < 0.2:
        parser.error("--poll must be at least 0.2 s")
    return args


def load_billing(args, monitor: Monitor, keys: KeyStore, settings: dict[str, str]):
    """The billing service of --billing-config. Its package sits next to this file and
    is imported only here, so the monitor without billing never loads it. Links use
    GUFO_PUBLIC_URL, never a request's Host header; without it, this listener's address."""
    here = str(HERE)
    if here not in sys.path:
        sys.path.insert(0, here)
    import billing
    from billing import db as billing_db

    host = f"[{args.bind}]" if ":" in args.bind else args.bind
    config = billing.load_config(args.billing_config, settings.get("GUFO_PUBLIC_URL"),
                                 settings.get("GUFO_API_URL"), f"http://{host}:{args.port}")
    if not any(billing_db.key_owner(key["name"]) is None for key in keys.list()):
        raise ValueError(f"{keys.path} needs an owner key besides portal keys (u<id>-<n>)")

    def served_model():
        status, models = monitor.status, monitor.models
        return status.get("model") or (models[0]["id"] if models else None)

    return billing.Billing(config, keys, served_model)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        api_key = read_api_key(args.gufo_key_file) if args.gufo_key_file else None
    except (OSError, ValueError) as error:
        print(f"gufo-monitor: --gufo-key-file: {error}", file=sys.stderr)
        return 2
    monitor = Monitor(GufoClient(args.gufo, api_key), Telemetry(args.gpu_card),
                      poll_s=args.poll, history_s=args.history)
    try:
        settings = load_environment(args.env_file)
    except (OSError, ValueError) as error:
        print(f"gufo-monitor: --env-file: {error}", file=sys.stderr)
        return 2
    try:
        monitor.stats = StatsStore(args.stats_file, args.poll)
    except (OSError, sqlite3.Error) as error:
        print(f"gufo-monitor: --stats-file: {error}", file=sys.stderr)
        return 2
    admin = AdminAuth(load_or_create_token(args.admin_token_file))
    keys = None
    if args.keys_file:
        keys = KeyStore(args.keys_file)
        try:
            keys.list()
        except (OSError, KeyFileError) as error:
            print(f"gufo-monitor: --keys-file: {error}", file=sys.stderr)
            return 2
    service = None
    if args.billing_config:
        try:
            service = load_billing(args, monitor, keys, settings)
        except (OSError, ValueError, sqlite3.Error) as error:
            print(f"gufo-monitor: --billing-config: {error}", file=sys.stderr)
            return 2
        monitor.billing = service
    server = Server((args.bind, args.port), monitor, admin, args.secure_cookie, keys, service)
    threads = [threading.Thread(target=monitor.run, args=(server.stop,), daemon=True,
                                name="poll")]
    if args.log_source != "none":
        tail = LogTail(args.log_source, monitor, args.show_clients, args.log_utc,
                       args.log_since)
        threads.append(threading.Thread(target=tail.run, args=(server.stop,), daemon=True,
                                        name="log"))
    if args.log_source.startswith("file:") and monitor.stats.needs_backfill():
        log_path = Path(args.log_source.partition(":")[2])

        def backfill():  # once, beside the tail: the store counts each request once
            try:
                added = backfill_stats(monitor.stats, log_path, args.log_utc, server.stop)
            except (OSError, sqlite3.Error) as error:
                print(f"gufo-monitor: stats backfill: {error}", file=sys.stderr)
                return
            print(f"gufo-monitor: stats backfill: {added} requests from {log_path}",
                  file=sys.stderr)

        threads.append(threading.Thread(target=backfill, daemon=True, name="stats-backfill"))
    if service is not None:
        threads.append(threading.Thread(target=service.run, args=(server.stop,), daemon=True,
                                        name="billing"))
    for thread in threads:
        thread.start()
    if service is not None:
        print(f"gufo-monitor: portal links use {service.config['public_base_url']}",
              file=sys.stderr)
    print(f"gufo-monitor: http://{args.bind}:{args.port}/ (monitor at /monitor) watching "
          f"{args.gufo}", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop.set()
        with monitor.cond:
            monitor.cond.notify_all()
        server.server_close()
        try:
            monitor.stats.flush()
        except sqlite3.Error as error:
            print(f"gufo-monitor: --stats-file: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
