#!/usr/bin/env python3
"""Turn a Hugging Face speech dataset (parquet with embedded audio) into the JSONL that the upstream
`prepare_data.py` reads: {"audio": wav path, "text": ..., "ref_audio": wav path}.

    build_dataset.py --parquet 'DIR/data/*.parquet' --text-column text_normalized --out DIR/train_raw \
        [--speaker-column speaker_id --speaker plamen_sivov] [--language-column language --language bg]

Clips are resampled to 24 kHz mono 16-bit WAV. A clip is dropped when its text is empty, it is too short
or too long, its text is not mostly Cyrillic/Latin letters of the wanted script, or the characters per
second fall outside the band that real speech occupies (a mismatch there almost always means the
transcript does not belong to the audio). Every clip gets the same reference clip, the cleanest-sounding
mid-length one, because the upstream script derives the speaker embedding from it. `--holdout N` clips are
kept out of training and written to `heldout.jsonl`, to listen to after training.
"""

import argparse
import glob
import io
import json
import math
import random
import re
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import soundfile as sf
from scipy.signal import resample_poly

RATE = 24000


def keep_clip(text, seconds, min_seconds, max_seconds, min_cps, max_cps, script):
    """Why a clip is unusable, or None when it is fine."""
    text = (text or "").strip()
    if not text:
        return "empty text"
    if not min_seconds <= seconds <= max_seconds:
        return "duration"
    cps = len(text) / seconds
    if not min_cps <= cps <= max_cps:
        return "chars per second"
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "no letters"
    wanted = sum(1 for c in letters if re.match(script, c))
    if wanted / len(letters) < 0.9:
        return "script"
    return None


def to_rate(samples, rate):
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if rate != RATE:
        g = math.gcd(rate, RATE)
        samples = resample_poly(samples, RATE // g, rate // g)
    return np.clip(samples, -1.0, 1.0).astype(np.float32)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--parquet", required=True, help="glob of parquet files")
    p.add_argument("--out", required=True, help="output directory (wavs/, train_raw.jsonl, heldout.jsonl)")
    p.add_argument("--audio-column", default="audio")
    p.add_argument("--text-column", required=True)
    p.add_argument("--speaker-column")
    p.add_argument("--speaker", help="keep only this value of --speaker-column")
    p.add_argument("--language-column")
    p.add_argument("--language", help="keep only this value of --language-column")
    p.add_argument("--script", default=r"[\u0400-\u04FF]", help="regex one letter of the target script matches")
    p.add_argument("--min-seconds", type=float, default=2.0)
    p.add_argument("--max-seconds", type=float, default=15.0)
    p.add_argument("--min-cps", type=float, default=7.0)
    p.add_argument("--max-cps", type=float, default=21.0)
    p.add_argument("--holdout", type=int, default=50)
    p.add_argument("--seed", type=int, default=1234)
    args = p.parse_args()

    out = Path(args.out)
    (out / "wavs").mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(args.parquet))
    if not files:
        raise SystemExit(f"no files match {args.parquet}")

    columns = [args.audio_column, args.text_column] + [c for c in (args.speaker_column, args.language_column) if c]
    rows, dropped = [], {}
    index = 0
    for path in files:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=128, columns=columns):
            data = batch.to_pydict()
            for i in range(len(data[args.text_column])):
                if args.speaker and data[args.speaker_column][i] != args.speaker:
                    continue
                if args.language and data[args.language_column][i] != args.language:
                    continue
                samples, rate = sf.read(io.BytesIO(data[args.audio_column][i]["bytes"]), dtype="float32")
                samples = to_rate(samples, rate)
                seconds = len(samples) / RATE
                text = data[args.text_column][i]
                why = keep_clip(text, seconds, args.min_seconds, args.max_seconds,
                                args.min_cps, args.max_cps, args.script)
                if why:
                    dropped[why] = dropped.get(why, 0) + 1
                    continue
                wav = out / "wavs" / f"{index:06d}.wav"
                sf.write(wav, samples, RATE, subtype="PCM_16")
                rms = float(np.sqrt(np.mean(samples ** 2)))
                rows.append({"audio": str(wav), "text": text.strip(), "seconds": seconds, "rms": rms})
                index += 1
    if not rows:
        raise SystemExit("nothing left after filtering")

    # Reference clip: mid-length (6-10 s) with a typical, not extreme, loudness, so it is neither
    # whispered nor clipped. Fixed for the whole dataset.
    mid = [r for r in rows if 6.0 <= r["seconds"] <= 10.0] or rows
    median_rms = float(np.median([r["rms"] for r in rows]))
    reference = min(mid, key=lambda r: abs(r["rms"] - median_rms))["audio"]

    random.Random(args.seed).shuffle(rows)
    held = [r for r in rows if r["audio"] != reference][: args.holdout]
    held_paths = {r["audio"] for r in held}
    train = [r for r in rows if r["audio"] not in held_paths]

    def write(name, items):
        with open(out / name, "w", encoding="utf-8") as f:
            for r in items:
                f.write(json.dumps({"audio": r["audio"], "text": r["text"], "ref_audio": reference},
                                   ensure_ascii=False) + "\n")

    write("train_raw.jsonl", train)
    write("heldout.jsonl", held)
    hours = sum(r["seconds"] for r in train) / 3600
    print(f"{len(train)} training clips, {hours:.2f} h; {len(held)} held out; reference {reference}")
    print("dropped:", dropped or "nothing")


if __name__ == "__main__":
    main()
