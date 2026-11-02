#!/usr/bin/env python3
"""Make Qwen3-ASR fine-tuning data (16 kHz mono WAV files and a JSONL) from FLEURS or Common Voice downloads.

    prepare_data.py fleurs  --root DIR/fleurs/data/bg_bg --split train --out OUT --language Bulgarian
    prepare_data.py commonvoice --root DIR/cv22 --lang bg --split dev --out OUT --language Bulgarian

Writes OUT/<split>.jsonl with {"audio": path, "text": "language Bulgarian<asr_text>REFERENCE"} per clip, the layout
QwenLM/Qwen3-ASR finetuning/qwen3_asr_sft.py reads, and OUT/<split>-plain.jsonl without the prefix (for wer.py).
The reference keeps its casing and punctuation, which is what the model writes.
"""

import argparse
import csv
import io
import json
import tarfile
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

RATE = 16000
MAX_SECONDS = 30.0


def to_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if rate != RATE:
        g = gcd(rate, RATE)
        samples = resample_poly(samples, RATE // g, rate // g)
    return np.clip(samples, -1.0, 1.0).astype(np.float32)


def fleurs_rows(root: Path, split: str):
    """(clip name, reference, wav bytes) from FLEURS's tsv and its audio tarball."""
    references = {}
    for line in (root / f"{split}.tsv").read_text(encoding="utf-8").splitlines():
        cells = line.split("\t")
        if len(cells) == 7:
            references[cells[1]] = cells[2]
    with tarfile.open(root / "audio" / f"{split}.tar.gz") as tar:
        for member in tar:
            name = Path(member.name).name
            if member.isfile() and name in references:
                yield name, references[name], tar.extractfile(member).read()


def commonvoice_rows(root: Path, lang: str, split: str):
    references = {}
    with open(root / "transcript" / lang / f"{split}.tsv", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            references[row["path"]] = row["sentence"]
    for tar_path in sorted((root / "audio" / lang / split).glob("*.tar")):
        with tarfile.open(tar_path) as tar:
            for member in tar:
                name = Path(member.name).name
                if member.isfile() and name in references:
                    yield name, references[name], tar.extractfile(member).read()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("source", choices=["fleurs", "commonvoice"])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--lang", default="bg")
    p.add_argument("--split", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--language", required=True, help="the language name the model writes, e.g. Bulgarian")
    args = p.parse_args()

    rows = (fleurs_rows(args.root, args.split) if args.source == "fleurs"
            else commonvoice_rows(args.root, args.lang, args.split))
    wavs = args.out / f"{args.source}-{args.split}"
    wavs.mkdir(parents=True, exist_ok=True)
    kept = skipped = 0
    seconds = 0.0
    with open(args.out / f"{args.source}-{args.split}.jsonl", "w", encoding="utf-8") as f, \
            open(args.out / f"{args.source}-{args.split}-plain.jsonl", "w", encoding="utf-8") as plain:
        for name, reference, data in rows:
            reference = reference.strip()
            try:
                samples, rate = sf.read(io.BytesIO(data), dtype="float32")
            except (RuntimeError, sf.LibsndfileError):
                skipped += 1
                continue
            samples = to_16k(samples, rate)
            if not reference or not 0.5 <= len(samples) / RATE <= MAX_SECONDS:
                skipped += 1
                continue
            path = wavs / (Path(name).stem + ".wav")
            sf.write(path, samples, RATE, subtype="PCM_16")
            f.write(json.dumps({"audio": str(path), "text": f"language {args.language}<asr_text>{reference}"},
                               ensure_ascii=False) + "\n")
            plain.write(json.dumps({"audio": str(path), "text": reference}, ensure_ascii=False) + "\n")
            kept += 1
            seconds += len(samples) / RATE
    print(f"{kept} clips, {seconds / 3600:.2f} h; {skipped} skipped")


if __name__ == "__main__":
    main()
