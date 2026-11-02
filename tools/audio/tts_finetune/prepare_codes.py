#!/usr/bin/env python3
"""Add `audio_codes` to every row of a JSONL file: the upstream prepare_data.py with progress output and
resumability. The upstream script prints nothing and writes its file only at the very end, so an
interrupted hour is lost.

    run.sh prepare_codes.py --tokenizer-model DIR/Qwen3-TTS-Tokenizer-12Hz --input train_raw.jsonl --output train.jsonl

Rows already present in --output (matched by `audio` path) are kept; rerun the same command to continue.
"""

import argparse
import json
import os
import time
from pathlib import Path

# MIOpen tunes convolutions for every new input length. With the default mode the encoder stalled for
# minutes to hours on some batches (17 rows/s dropped to 4 rows/s average, and once to nothing in an hour);
# with FAST it ran at a steady 17 rows/s. This is the observed fix, not a diagnosed cause.
os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")

from qwen_tts import Qwen3TTSTokenizer

BATCH = 32


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tokenizer-model", required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--device", default="cuda:0")
    p.add_argument("--limit", type=int, default=0, help="only the first N rows (for timing)")
    args = p.parse_args()

    rows = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[: args.limit]
    out = Path(args.output)
    done = set()
    if out.exists():
        done = {json.loads(line)["audio"] for line in out.read_text(encoding="utf-8").splitlines() if line.strip()}
    todo = [r for r in rows if r["audio"] not in done]
    print(f"{len(done)} rows already done, {len(todo)} to go", flush=True)

    tokenizer = Qwen3TTSTokenizer.from_pretrained(args.tokenizer_model, device_map=args.device)
    started = time.monotonic()
    with open(out, "a", encoding="utf-8") as f:
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            encoded = tokenizer.encode([r["audio"] for r in batch])
            for row, codes in zip(batch, encoded.audio_codes):
                row["audio_codes"] = codes.cpu().tolist()
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            finished = i + len(batch)
            elapsed = time.monotonic() - started
            rate = finished / elapsed
            print(f"{finished}/{len(todo)} rows  {rate:.1f} rows/s  remaining {int((len(todo) - finished) / rate)} s",
                  flush=True)


if __name__ == "__main__":
    main()
