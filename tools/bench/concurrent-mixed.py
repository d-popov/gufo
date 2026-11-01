#!/usr/bin/env python3
"""Concurrent sessions, alone and mixed: decode with N sessions, prefill with N sessions, and
decoding sessions while others prefill long prompts.

Run against a `gufo serve llm` server over HTTP (streaming). Every request carries a unique
prompt (random words, cache off) so no session reuses another's prefix. The server streams one
piece per generated token, even when speculation accepts a block, so a window's token count is the
number of pieces that arrived in it. That is only trusted when a run's pieces match the token count
the server reports for it (within 1%); a run that does not match makes its rates invalid, and an
invalid window reports no rate. Windows must be covered by every session they average over.

    OPENAI_API_KEY=... concurrent-mixed.py --model SUB [--big-model BIG] --max-sessions 8 \\
        --output artifacts/mixed.json

With --big-model, the first session of a run uses it (a larger session class); the rest use
--model. Words are drawn from /usr/share/dict/words, or a generated list when absent.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

WORDS_FILE = Path("/usr/share/dict/words")


def load_words() -> list[str]:
    if WORDS_FILE.exists():
        return WORDS_FILE.read_text().split()
    return [f"w{i}" for i in range(5000)]


WORDS = load_words()


def document(seed: int, words: int, ask: str) -> str:
    rnd = random.Random(seed)
    body = " ".join(rnd.choice(WORDS) for _ in range(words))
    return f"Document {seed}:\n{body}\n\n{ask}"


@dataclass
class Run:
    """One streamed request: what it sent, when each piece came back, and what the server counted."""

    name: str
    model: str
    prompt: str
    max_tokens: int
    started: float = 0.0
    first: float | None = None
    finished: float = 0.0
    pieces: list[float] = field(default_factory=list)  # arrival time of each streamed piece
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    def go(self, base_url: str, key: str) -> None:
        body = {"model": self.model, "stream": True, "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": self.prompt}], "max_tokens": self.max_tokens,
                "temperature": 0, "cache_prompt": False,
                "chat_template_kwargs": {"enable_thinking": False}}
        request = urllib.request.Request(
            base_url + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        self.started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=1800) as response:
                for raw in response:
                    line = raw.decode().strip()
                    if not line.startswith("data:") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[5:])
                    usage = event.get("usage")
                    if usage:
                        self.prompt_tokens = usage["prompt_tokens"]
                        self.completion_tokens = usage["completion_tokens"]
                    for choice in event.get("choices", []):
                        text = choice.get("delta", {}).get("content") or ""
                        if text:
                            now = time.monotonic()
                            self.first = self.first or now
                            self.pieces.append(now)
        except Exception as error:  # reported with the run; one failed request must not hide the rest
            self.error = f"{type(error).__name__}: {error}"
        self.finished = time.monotonic()

    # -- readings

    @property
    def counted(self) -> bool:
        """Whether the pieces are the tokens: one per token, as the server reports them."""
        return (self.error is None and self.completion_tokens > 0
                and abs(len(self.pieces) - self.completion_tokens) <= 0.01 * self.completion_tokens)

    def tokens_between(self, start: float, end: float) -> int:
        return sum(1 for t in self.pieces if start <= t < end)

    def longest_gap(self, start: float, end: float) -> float:
        """The longest time within [start, end] with no piece, counting from the window's edges."""
        times = [start] + [t for t in self.pieces if start <= t < end] + [end]
        return max(b - a for a, b in zip(times, times[1:]))


def run_all(runs: list[Run], base_url: str, key: str, delays: list[float] | None = None) -> None:
    threads = []
    t0 = time.monotonic()
    for i, run in enumerate(runs):
        delay = (delays[i] if delays else 0.0)

        def start(run=run, delay=delay):
            wait = t0 + delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            run.go(base_url, key)

        thread = threading.Thread(target=start)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


def models_for(count: int, model: str, big_model: str | None) -> list[str]:
    return [big_model if (big_model and i == 0) else model for i in range(count)]


def invalid_reason(runs: list[Run], start: float | None, end: float) -> str:
    if any(r.error for r in runs):
        return "a request failed"
    if not all(r.counted for r in runs):
        return "streamed pieces do not match the server's token counts: " + ", ".join(
            f"{r.name} {len(r.pieces)} pieces vs {r.completion_tokens} tokens" for r in runs if not r.counted)
    return "the sessions did not generate together for 3 s"


class Bench:
    def __init__(self, args):
        self.args = args
        self.key = os.environ["OPENAI_API_KEY"]
        self.seed = args.seed
        self.results: dict = {"decode": [], "prefill": [], "mixed": []}

    def next_seed(self) -> int:
        self.seed += 1
        return self.seed

    def failed(self, runs: list[Run]) -> list[str]:
        return [f"{r.name}: {r.error}" for r in runs if r.error]

    def decode(self, n: int) -> dict:
        """N sessions, short prompts, long generations, all at once."""
        runs = [Run(f"d{i}", m, document(self.next_seed(), 40, "Write a long, detailed story, continuing until the limit."),
                    self.args.decode_tokens)
                for i, m in enumerate(models_for(n, self.args.model, self.args.big_model))]
        run_all(runs, self.args.base_url, self.key)
        # The window where all sessions were generating: from the last first piece to the first finish.
        start = max((r.first for r in runs if r.first), default=None)
        end = min(r.finished for r in runs)
        valid = start is not None and end - start >= 3.0 and all(r.counted for r in runs)
        record = {"sessions": n, "valid": valid, "errors": self.failed(runs)}
        if valid:
            per = [r.tokens_between(start, end) / (end - start) for r in runs]
            record.update(window_s=round(end - start, 1), per_session_tok_s=[round(x, 2) for x in per],
                          total_tok_s=round(sum(per), 2))
        else:
            record["why"] = invalid_reason(runs, start, end)
        self.results["decode"].append(record)
        return record

    def prefill(self, n: int) -> dict:
        """N sessions each given a long prompt, one output token."""
        words = self.args.prefill_words
        runs = [Run(f"p{i}", m, document(self.next_seed(), words, "Reply with the single word OK."), 1)
                for i, m in enumerate(models_for(n, self.args.model, self.args.big_model))]
        t0 = time.monotonic()
        run_all(runs, self.args.base_url, self.key)
        wall = time.monotonic() - t0
        tokens = sum(r.prompt_tokens for r in runs)
        record = {"sessions": n, "prompt_tokens_each": [r.prompt_tokens for r in runs], "wall_s": round(wall, 1),
                  "total_prefill_tok_s": round(tokens / wall, 1), "errors": self.failed(runs)}
        self.results["prefill"].append(record)
        return record

    def mixed(self, generators: int, prefillers: int) -> dict:
        """Generators decode steadily; after a while the prefillers submit long prompts together."""
        models = models_for(generators + prefillers, self.args.model, self.args.big_model)
        gens = [Run(f"g{i}", models[i], document(self.next_seed(), 40, "Write a long, detailed story, continuing until the limit."),
                    self.args.mixed_tokens) for i in range(generators)]
        pres = [Run(f"p{i}", models[generators + i], document(self.next_seed(), self.args.prefill_words, "Reply with the single word OK."), 1)
                for i in range(prefillers)]
        lead = self.args.lead_s
        runs = gens + pres
        run_all(runs, self.args.base_url, self.key, [0.0] * generators + [lead] * prefillers)
        pre_start = min(r.started for r in pres)
        pre_end = max(r.finished for r in pres)
        gen_end = min(r.finished for r in gens)
        steady = max((r.first for r in gens if r.first), default=None)
        counted = all(r.counted for r in gens) and not any(r.error for r in pres)

        def window(a: float | None, b: float, minimum: float) -> dict | None:
            """Generator rates over [a, b], only when every generator spans it and it is long enough."""
            if a is None or b - a < minimum or b > gen_end or any(not g.first or g.first > a for g in gens):
                return None
            total = sum(g.tokens_between(a, b) for g in gens) / (b - a)
            return {"seconds": round(b - a, 1), "generator_total_tok_s": round(total, 2),
                    "longest_gap_s": round(max(g.longest_gap(a, b) for g in gens), 2)}

        record = {"generators": generators, "prefillers": prefillers, "errors": self.failed(runs),
                  "prefill_wall_s": round(pre_end - pre_start, 1),
                  "prefill_tok_s": round(sum(p.prompt_tokens for p in pres) / max(pre_end - pre_start, 1e-9), 1)}
        if steady is not None:
            steady += 5.0  # past the start-up, before the prefills
        windows = {"alone": window(steady, pre_start, 5.0), "during_prefill": window(pre_start, pre_end, 1.0),
                   "after_prefill": window(pre_end, gen_end, 5.0)}
        record["valid"] = counted and windows["alone"] is not None and windows["during_prefill"] is not None
        if counted:
            record.update(windows)
        if not record["valid"]:
            record["why"] = (invalid_reason(gens + pres, steady, gen_end) if not counted else
                             "the generators did not span the whole prefill (raise --mixed-tokens or lower the prompt)")
        self.results["mixed"].append(record)
        return record

    def warm(self) -> None:
        run = Run("warm", self.args.model, document(1, 20, "Say hi."), 8)
        run.go(self.args.base_url, self.key)
        if run.error:
            sys.exit(f"the server did not answer: {run.error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:1234")
    parser.add_argument("--model", required=True, help="served model of the sessions (a session class)")
    parser.add_argument("--big-model", help="served model of the first session, for a larger class")
    parser.add_argument("--max-sessions", type=int, default=8)
    parser.add_argument("--decode-tokens", type=int, default=300)
    parser.add_argument("--mixed-tokens", type=int, default=700)
    parser.add_argument("--prefill-words", type=int, default=4500, help="about 12k tokens")
    parser.add_argument("--lead-s", type=float, default=25.0, help="how long generators run before prefills start")
    parser.add_argument("--seed", type=int, default=7000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    bench = Bench(args)
    bench.warm()
    counts = [n for n in (1, 2, 4, 8) if n <= args.max_sessions]
    for n in counts:
        print("decode", json.dumps(bench.decode(n)), flush=True)
    for n in counts:
        print("prefill", json.dumps(bench.prefill(n)), flush=True)
    shapes = [(1, 1), (2, 2), (4, 4), (6, 2), (2, 6)]
    for g, p in shapes:
        if g + p <= args.max_sessions:
            print("mixed", json.dumps(bench.mixed(g, p)), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"args": {k: str(v) for k, v in vars(args).items()}, **bench.results}, indent=1))


if __name__ == "__main__":
    main()
