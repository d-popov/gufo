#!/usr/bin/env python3
"""Sweep `--prefill-chunk` x `--decode-rounds` for one decoding stream beside a cold prefill.

The measurement behind the table in docs/SERVER.md: with one request decoding, a second
request arrives with a cold prompt of about 37.6K tokens. Per setting it reports the solo
prefill (tok/s), the solo decode rate, then both together: the prefill time of the arrival,
the decode rate of the stream that was already running, and its longest pause. It starts one
private server per setting, so nothing else may use the GPU.

The server is launched from `--serve-cmd`, a shell command with `{port}`, `{chunk}` and
`{rounds}` placeholders, so the caller supplies the container and the exact production flags.
Each server is stopped with `--stop-cmd` when given, else by ending its process group.

Both requests go to one session class, which must have at least two slots: with one slot the
arrival would queue behind the decoding stream and nothing interleaves. Servers with session
classes need `--model` (for Flash-Next production: `--model qwen3.8-flash-next-sub`, the 3-slot
class where concurrent agents run); a server with one model needs nothing. The class, its slots
and its context are recorded with every setting, because the best setting depends on that shape.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import random
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_GRID = "512x1,1024x4,1024x8,1024x16,2048x8,2048x16,4096x16"
STORY = ("Write a very long, detailed story about a lighthouse keeper and the ships he saw, "
         "in many chapters. Do not stop early.")
WORDS = ("amber bridge candle delta ember forest garden harbor island jungle kettle lantern meadow "
         "nectar orchard pebble quartz river saddle timber umber valley willow yonder zephyr").split()


def filler(seed: int, words: int) -> str:
    """Text that is unlike any other seed's from the first token, so no prefix is ever shared."""
    rng = random.Random(seed)
    return " ".join(f"{rng.choice(WORDS)}{rng.randrange(10_000)}" for _ in range(words))


class Stream:
    """One streamed chat completion, timed per piece of text, read on its own thread."""

    def __init__(self, port: int, prompt: str, max_tokens: int, model: str | None):
        self.port, self.max_tokens = port, max_tokens
        self.body = {"messages": [{"role": "user", "content": prompt}], "stream": True,
                     "stream_options": {"include_usage": True}, "max_tokens": max_tokens,
                     "temperature": 0, "cache_prompt": False, "reasoning_effort": "none"}
        if model:
            self.body["model"] = model
        self.sent = self.first = self.last = None
        self.pieces: list[tuple[float, int]] = []   # (time, characters in this piece)
        self.prompt_tokens = self.completion_tokens = None
        self.error: str | None = None
        self._conn: http.client.HTTPConnection | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "Stream":
        self.sent = time.monotonic()
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self._conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1800)
            self._conn.request("POST", "/v1/chat/completions", json.dumps(self.body),
                               {"Content-Type": "application/json"})
            response = self._conn.getresponse()
            if response.status != 200:
                self.error = f"HTTP {response.status}: {response.read(300)!r}"
                return
            for raw in response:
                if not raw.startswith(b"data:") or b"[DONE]" in raw:
                    continue
                chunk = json.loads(raw[5:])
                if chunk.get("usage"):
                    self.prompt_tokens = chunk["usage"].get("prompt_tokens")
                    self.completion_tokens = chunk["usage"].get("completion_tokens")
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    text = (delta.get("content") or "") + (delta.get("reasoning_content") or "")
                    if text:
                        now = time.monotonic()
                        self.first = self.first or now
                        self.last = now
                        self.pieces.append((now, len(text)))
        except (OSError, http.client.HTTPException, ValueError) as error:
            if not getattr(self, "_cancelled", False):
                self.error = repr(error)

    def cancel(self) -> None:
        self._cancelled = True
        conn = self._conn
        if conn is not None:
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(2)
            except OSError:
                pass
            conn.close()

    def join(self) -> "Stream":
        self._thread.join()
        return self

    def wait_for(self, characters: int, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.error:
            if sum(n for _, n in list(self.pieces)) >= characters:
                return True
            time.sleep(0.02)
        return False


def decode_window(stream: Stream, start: float, end: float) -> dict:
    """Characters per second and the longest pause between pieces inside [start, end]."""
    inside = [(t, n) for t, n in stream.pieces if start <= t <= end]
    edges = [start] + [t for t, _ in inside] + [end]
    gap = max((b - a for a, b in zip(edges, edges[1:])), default=end - start)
    chars = sum(n for _, n in inside)
    return {"chars_per_s": chars / (end - start) if end > start else 0.0, "longest_gap_s": gap,
            "pieces": len(inside)}


def wait_ready(port: int, process: subprocess.Popen, timeout: float) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with {process.returncode} before it was ready")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=3) as reply:
                if reply.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError("server was not ready in time")


def get_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as reply:
        return json.load(reply)


def select_class(port: int, wanted: str | None, min_slots: int) -> dict:
    """The model (session class) both requests use, and the shape being tuned."""
    served = [m["id"] for m in get_json(port, "/v1/models").get("data", []) if isinstance(m.get("id"), str)]
    if wanted is None and len(served) > 1:
        raise RuntimeError(f"the server has several session classes {served}: choose one with --model")
    if wanted is not None and wanted not in served:
        raise RuntimeError(f"--model {wanted!r} is not served; the server lists {served}")
    model = wanted or (served[0] if served else None)
    query = f"?model={urllib.parse.quote(model, safe='')}" if model and len(served) > 1 else ""
    props = get_json(port, "/props" + query)
    slots = props.get("total_slots")
    context = (props.get("default_generation_settings") or {}).get("n_ctx")
    if not isinstance(slots, int) or slots < min_slots:
        raise RuntimeError(f"class {model!r} has {slots} slot(s); the arrival would queue behind the "
                           f"decoding stream. Use a class with at least {min_slots}, or --min-slots 1 to measure queueing")
    return {"model": model, "slots": slots, "context": context}


def calibrate(port: int, model: str | None, target_tokens: int) -> int:
    """Words per prompt that reach about `target_tokens`, from one small request."""
    probe = Stream(port, filler(1, 3000), 1, model).start().join()
    if probe.error or not probe.prompt_tokens:
        raise RuntimeError(f"calibration failed: {probe.error or 'no usage in the reply'}")
    return int(3000 * target_tokens / probe.prompt_tokens)


def measure(port: int, model: str | None, words: int, seed: int, decode_tokens: int) -> dict:
    cold = lambda offset: filler(seed + offset, words)  # noqa: E731
    result: dict = {}
    # Solo prefill: time to the first token of a one-token answer.
    solo = Stream(port, cold(1), 1, model).start().join()
    if solo.error or solo.first is None:
        raise RuntimeError(f"solo prefill failed: {solo.error}")
    result["prompt_tokens"] = solo.prompt_tokens
    result["solo_prefill_s"] = solo.first - solo.sent
    result["solo_prefill_tps"] = solo.prompt_tokens / result["solo_prefill_s"]
    # Solo decode: the rate once the first piece is out.
    alone = Stream(port, STORY, 400, model).start().join()
    if alone.error or len(alone.pieces) < 10:
        raise RuntimeError(f"solo decode failed: {alone.error}")
    result["solo_decode_chars_per_s"] = decode_window(alone, alone.first, alone.last)["chars_per_s"]
    # Together: a stream that is already decoding, then the cold arrival.
    runner = Stream(port, STORY, decode_tokens, model).start()
    if not runner.wait_for(400, 120):
        runner.cancel()
        raise RuntimeError(f"the decoding stream did not start: {runner.error}")
    arrival = Stream(port, cold(2), 1, model).start()
    arrival.join()
    if arrival.error or arrival.first is None:
        runner.cancel()
        raise RuntimeError(f"arrival failed: {arrival.error}")
    window_end = arrival.first
    result["still_decoding"] = runner.last is not None and runner.last >= window_end - 1.0
    result["together_prefill_s"] = window_end - arrival.sent
    result["together_prefill_tps"] = arrival.prompt_tokens / result["together_prefill_s"]
    during = decode_window(runner, arrival.sent, window_end)
    result["decode_during_chars_per_s"] = during["chars_per_s"]
    result["longest_decode_gap_s"] = during["longest_gap_s"]
    runner.cancel()
    runner.join()
    return result


def run_setting(args, chunk: int, rounds: int, log_dir: Path) -> dict:
    port = args.port
    command = args.serve_cmd.format(port=port, chunk=chunk, rounds=rounds)
    log = open(log_dir / f"server-{chunk}x{rounds}.log", "wb")
    process = subprocess.Popen(command, shell=True, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    try:
        started = time.monotonic()
        wait_ready(port, process, args.ready_timeout)
        entry = {"prefill_chunk": chunk, "decode_rounds": rounds,
                 "ready_s": time.monotonic() - started, "command": command}
        shape = select_class(port, args.model, args.min_slots)
        entry["shape"] = {**shape, "streams": "1 decoding + 1 cold arrival, same class",
                          "prompt_tokens": args.prompt_tokens}
        words = calibrate(port, shape["model"], args.prompt_tokens)
        # One untimed pass warms kernels and allocators, so the first setting is not penalized.
        measure(port, shape["model"], max(words // 8, 100), 900, 200)
        samples = [measure(port, shape["model"], words, 1000 * (i + 1), args.decode_tokens)
                   for i in range(args.repetitions)]
        for key in samples[0]:
            values = [s[key] for s in samples]
            entry[key] = statistics.median(values) if isinstance(values[0], (int, float)) else values
        entry["samples"] = samples
        return entry
    finally:
        if args.stop_cmd:
            subprocess.run(args.stop_cmd.format(port=port), shell=True, check=False)
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
        log.close()
        time.sleep(args.settle)


def table(entries: list[dict]) -> str:
    head = ("| `--prefill-chunk` | `--decode-rounds` | Solo prefill tok/s | Prefill beside decode (s) | "
            "Prefill beside decode tok/s | Solo decode chars/s | Decode during prefill chars/s | "
            "Longest decode gap (s) |\n| --- | --- | --- | --- | --- | --- | --- | --- |")
    rows = [f"| {e['prefill_chunk']} | {e['decode_rounds']} | {e['solo_prefill_tps']:.0f} | "
            f"{e['together_prefill_s']:.1f} | {e['together_prefill_tps']:.0f} | "
            f"{e['solo_decode_chars_per_s']:.0f} | {e['decode_during_chars_per_s']:.1f} | "
            f"{e['longest_decode_gap_s']:.2f} |" + ("" if e["still_decoding"] else " (decode ended early)")
            for e in entries]
    return "\n".join([head, *rows])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve-cmd", required=True,
                        help="shell command that serves on {port} with --prefill-chunk {chunk} "
                             "--decode-rounds {rounds}")
    parser.add_argument("--stop-cmd", help="shell command that stops the server, with {port}")
    parser.add_argument("--grid", default=DEFAULT_GRID, help=f"CHUNKxROUNDS list (default {DEFAULT_GRID})")
    parser.add_argument("--model", help="session class both requests use (required when the server has "
                                        "several, as production does: qwen3.8-flash-next-sub)")
    parser.add_argument("--min-slots", type=int, default=2,
                        help="refuse a class with fewer slots, where the arrival queues (default 2)")
    parser.add_argument("--port", type=int, default=18234)
    parser.add_argument("--prompt-tokens", type=int, default=37600, help="cold prompt length")
    parser.add_argument("--decode-tokens", type=int, default=6000,
                        help="output tokens of the stream that is decoding when the prompt arrives")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--ready-timeout", type=float, default=900)
    parser.add_argument("--settle", type=float, default=5, help="seconds to wait between servers")
    parser.add_argument("--out", type=Path, default=Path("artifacts/prefill-decode-sweep.json"))
    args = parser.parse_args(argv)
    try:
        grid = [tuple(int(v) for v in item.split("x")) for item in args.grid.split(",")]
    except ValueError:
        parser.error("--grid is a comma list of CHUNKxROUNDS, such as 1024x8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    log_dir = args.out.parent / "prefill-decode-sweep-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for chunk, rounds in grid:
        print(f"[sweep] prefill-chunk {chunk}, decode-rounds {rounds}", flush=True)
        try:
            entries.append(run_setting(args, chunk, rounds, log_dir))
        except (RuntimeError, OSError) as error:
            print(f"[sweep]   failed: {error}", file=sys.stderr, flush=True)
            entries.append({"prefill_chunk": chunk, "decode_rounds": rounds, "error": str(error)})
        args.out.write_text(json.dumps({"date": time.strftime("%Y-%m-%d %H:%M:%S"), "entries": entries}, indent=1))
    good = [e for e in entries if "error" not in e]
    if good:
        shape = good[0]["shape"]
        print(f"\nShape: class {shape['model']} ({shape['slots']} slots, context {shape['context']}); "
              f"{shape['streams']}; cold prompt about {shape['prompt_tokens']} tokens.\n")
        print(table(good))
    return 0 if len(good) == len(entries) else 1


if __name__ == "__main__":
    sys.exit(main())
