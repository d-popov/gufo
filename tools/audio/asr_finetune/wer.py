#!/usr/bin/env python3
"""Word and character error rates of a speech-recognition server on a JSONL test set.

    wer.py --jsonl test.jsonl --url http://127.0.0.1:1235 [--api-key-file F] [--model NAME] [--limit N]

Each row is {"audio": wav path, "text": reference}; a leading `language X<asr_text>` prefix on the reference is
ignored. The server is any OpenAI-style /v1/audio/transcriptions endpoint (`gufo serve asr` is one). Both sides
are lower-cased and stripped of punctuation before they are compared, so casing and commas do not count.
Prints the corpus WER and CER, the languages the server said it heard, and a few worst rows.
"""

import argparse
import collections
import json
import re
import urllib.request
import uuid
from pathlib import Path

PREFIX = re.compile(r"^language [^<]*<asr_text>")


def normalize(text: str) -> str:
    text = PREFIX.sub("", text).lower()
    text = re.sub(r"[^\w\s]|_", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def edit_distance(a, b) -> int:
    """Levenshtein distance between two sequences."""
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def error_rates(pairs) -> dict:
    """Corpus WER and CER over (reference, hypothesis) pairs; the errors and lengths are summed first."""
    word_errors = word_total = char_errors = char_total = 0
    for reference, hypothesis in pairs:
        r, h = normalize(reference), normalize(hypothesis)
        word_errors += edit_distance(r.split(), h.split())
        word_total += len(r.split())
        char_errors += edit_distance(r, h)
        char_total += len(r)
    return {"wer": word_errors / max(word_total, 1), "cer": char_errors / max(char_total, 1),
            "words": word_total, "chars": char_total}


def transcribe(url: str, wav: bytes, key, model) -> dict:
    boundary = uuid.uuid4().hex
    parts = ([("model", model)] if model else []) + [("response_format", "verbose_json")]
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode() for n, v in parts)
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode() + wav + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url.rstrip("/") + "/v1/audio/transcriptions", data=body, headers=headers)
    return json.loads(urllib.request.urlopen(request, timeout=300).read())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jsonl", type=Path, required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--api-key-file", type=Path)
    p.add_argument("--model")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--worst", type=int, default=5)
    args = p.parse_args()
    key = args.api_key_file.read_text().strip() if args.api_key_file else None
    rows = [json.loads(line) for line in args.jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = rows[: args.limit] if args.limit else rows
    pairs, heard, scored = [], collections.Counter(), []
    for i, row in enumerate(rows, 1):
        answer = transcribe(args.url, Path(row["audio"]).read_bytes(), key, args.model)
        pairs.append((row["text"], answer["text"]))
        heard[answer.get("language") or "?"] += 1
        scored.append((error_rates([pairs[-1]])["wer"], row["text"], answer["text"]))
        if i % 100 == 0:
            print(f"{i}/{len(rows)}", flush=True)
    print(json.dumps({**error_rates(pairs), "clips": len(rows), "languages_heard": dict(heard)}, ensure_ascii=False))
    for wer, reference, hypothesis in sorted(scored, reverse=True)[: args.worst]:
        print(f"\nWER {wer:.2f}\n  ref: {normalize(reference)}\n  hyp: {normalize(hypothesis)}")


if __name__ == "__main__":
    main()
