#!/usr/bin/env python3
"""gufo-monitor: a read-only web monitor for a running `gufo serve llm`.

One background thread polls Gufo (/ready, /metrics, /slots; /v1/models and
/props less often) and the GPU/host telemetry in sysfs and /proc, another
tails Gufo's log for completed requests. Browsers read the shared state from
GET /api/state (snapshot with history) and GET /api/stream (Server-Sent
Events, one event per poll), so any number of browsers cost one set of polls.

The Gufo API key stays in this process: it is never sent to browsers, written
to responses or logged. Browsers authenticate with a separate monitor token.

Python 3.11+ standard library only.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import http.server
import json
import math
import os
import re
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
}
COOKIE_NAME = "gufo_monitor"
COOKIE_MAX_AGE = 30 * 24 * 3600
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

# Completed-request fields copied from the log; everything else is dropped so
# unknown or sensitive keys never reach the page.
REQUEST_FIELDS = (
    "request", "model", "method", "path", "status", "outcome", "duration_ms", "prompt_tokens",
    "prefill_tokens", "cached_tokens", "generated_tokens", "finish", "cache", "queue_ms",
    "ttft_ms", "prefill_tps", "decode_tps", "batch_width", "plan", "draft_accepted",
    "draft_proposed", "acceptance_pct",
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
    return {"t": t, "level": match["level"], "component": match["component"], "fields": fields}


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
            if isinstance(status, int) and status >= 400:
                self.log_totals["errors"] += 1

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
    print(f"gufo-monitor: created monitor token in {path}", file=sys.stderr)
    return token


def read_api_key(path: Path) -> str:
    key = path.read_text().strip()
    if not key:
        raise ValueError(f"{path} is empty")
    return key


class Auth:
    def __init__(self, token: str):
        self._token = token.encode()
        self.session = hmac.new(self._token, b"gufo-monitor-session-v1",
                                hashlib.sha256).hexdigest()

    def check_token(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._token)

    def check_request(self, headers) -> bool:
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer ") and self.check_token(authorization[7:].strip()):
            return True
        for part in headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and hmac.compare_digest(value.encode(), self.session.encode()):
                return True
        return False


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, monitor: Monitor, auth: Auth, secure_cookie: bool):
        super().__init__(address, Handler)
        self.monitor = monitor
        self.auth = auth
        self.secure_cookie = secure_cookie
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

    def _authorized(self) -> bool:
        return self.server.auth.check_request(self.headers)

    def _secure(self) -> bool:
        return self.server.secure_cookie or self.headers.get("X-Forwarded-Proto", "") == "https"

    def _cookie(self, value: str, max_age: int) -> tuple[str, str]:
        cookie = (f"{COOKIE_NAME}={value}; Path=/; Max-Age={max_age}; HttpOnly; "
                  "SameSite=Strict")
        return ("Set-Cookie", cookie + ("; Secure" if self._secure() else ""))

    # -- routes

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/login":
            return self._login_page(error=False)
        if path == "/favicon.ico":
            return self._send(204, b"", "text/plain")
        if not self._authorized():
            if path.startswith("/api/"):
                return self._json(401, b'{"error":"unauthorized"}')
            return self._redirect("/login")
        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(200, self.server.monitor.snapshot())
        if path == "/api/stream":
            return self._stream()
        self._json(404, b'{"error":"not_found"}')

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/login":
            return self._login()
        if path == "/logout":
            return self._redirect("/login", (self._cookie("", 0),))
        self._json(404, b'{"error":"not_found"}')

    def _file(self, name: str, content_type: str, cache: str = "no-store"):
        try:
            body = (STATIC_DIR / name).read_bytes()
        except OSError:
            return self._json(404, b'{"error":"not_found"}')
        self._send(200, body, content_type, cache=cache)

    def _static(self, name: str):
        content_type = STATIC_FILES.get(name)  # whitelist: no paths, no traversal
        if content_type is None:
            return self._json(404, b'{"error":"not_found"}')
        self._file(name, content_type, cache="no-cache")

    def _login_page(self, error: bool, status: int = 200):
        try:
            body = (STATIC_DIR / "login.html").read_text()
        except OSError:
            return self._json(500, b'{"error":"missing login page"}')
        message = '<p class="login-error">Wrong token.</p>' if error else ""
        self._send(status, body.replace("<!--ERROR-->", message).encode(),
                   "text/html; charset=utf-8")

    def _login(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            return self._login_page(error=True, status=400)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        token = (form.get("token") or [""])[0].strip()
        if not self.server.auth.check_token(token):
            time.sleep(0.5)
            return self._login_page(error=True, status=401)
        self._redirect("/", (self._cookie(self.server.auth.session, COOKIE_MAX_AGE),))

    def _stream(self):
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
                seq, payload = monitor.wait_tick(seq, 15.0)
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
    parser.add_argument("--bind", default="127.0.0.1", help="listen address")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--token-file", type=Path, default=config / "gufo-monitor" / "token",
                        help="monitor token for browsers; created (mode 0600) if missing")
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
                        help="always mark the login cookie Secure (served over https)")
    args = parser.parse_args(argv)
    if args.poll < 0.2:
        parser.error("--poll must be at least 0.2 s")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    token = load_or_create_token(args.token_file)
    try:
        api_key = read_api_key(args.gufo_key_file) if args.gufo_key_file else None
    except (OSError, ValueError) as error:
        print(f"gufo-monitor: --gufo-key-file: {error}", file=sys.stderr)
        return 2
    monitor = Monitor(GufoClient(args.gufo, api_key), Telemetry(args.gpu_card),
                      poll_s=args.poll, history_s=args.history)
    server = Server((args.bind, args.port), monitor, Auth(token), args.secure_cookie)
    threads = [threading.Thread(target=monitor.run, args=(server.stop,), daemon=True,
                                name="poll")]
    if args.log_source != "none":
        tail = LogTail(args.log_source, monitor, args.show_clients, args.log_utc,
                       args.log_since)
        threads.append(threading.Thread(target=tail.run, args=(server.stop,), daemon=True,
                                        name="log"))
    for thread in threads:
        thread.start()
    print(f"gufo-monitor: http://{args.bind}:{args.port}/ watching {args.gufo}", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop.set()
        with monitor.cond:
            monitor.cond.notify_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
