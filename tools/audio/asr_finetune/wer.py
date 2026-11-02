#!/usr/bin/env python3
"""Word and character error rates of a speech-recognition server on a JSONL test set.

    wer.py --jsonl test.jsonl --url http://127.0.0.1:1235 [--api-key-file F] [--model NAME] [--limit N] [--out run.jsonl]
    wer.py --rescore run.jsonl [--limit N]    # the same scores again from a saved run, without the server

Each row is {"audio": wav path, "text": reference}; a leading `language X<asr_text>` prefix on the reference is
ignored. The server is any OpenAI-style /v1/audio/transcriptions endpoint (`gufo serve asr` is one). Both sides
are lower-cased and stripped of punctuation before they are compared, so casing and commas do not count.
Prints the corpus WER and CER, the languages the server said it heard, and a few worst rows. `--out FILE` saves every
clip (reference, hypothesis, language) as it is done and a summary beside it, continues an interrupted run, and lets
`--rescore FILE` compute the scores of any subset later without the model.
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


def read_results(path: Path) -> list:
    """The clips a run saved, one JSON object per line."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize(results: list, worst: int = 0) -> dict:
    """The corpus scores of saved results, the languages heard, and the worst clips."""
    rates = error_rates([(r["reference"], r["hypothesis"]) for r in results])
    heard = collections.Counter(r.get("language") or "?" for r in results)
    ranked = sorted(results, key=lambda r: error_rates([(r["reference"], r["hypothesis"])])["wer"], reverse=True)
    return {**rates, "clips": len(results), "languages_heard": dict(heard), "worst": ranked[:worst]}


def score(rows: list, transcribe_clip, out: Path | None = None, progress=lambda done, total: None) -> list:
    """Transcribe each row and return the results. With `out`, every clip is appended to that file as soon as it is
    done, and clips already in it are not transcribed again, so an interrupted run continues and a finished one can
    be scored again (see --rescore) without the model."""
    saved = read_results(out) if out else []
    done = {r["audio"] for r in saved}
    results = [r for r in saved if r["audio"] in {row["audio"] for row in rows}]
    handle = open(out, "a", encoding="utf-8") if out else None
    try:
        for i, row in enumerate(rows, 1):
            if row["audio"] in done:
                continue
            answer = transcribe_clip(row["audio"])
            result = {"audio": row["audio"], "reference": row["text"], "hypothesis": answer["text"],
                      "language": answer.get("language")}
            results.append(result)
            if handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
            if i % 100 == 0:
                progress(i, len(rows))
    finally:
        if handle:
            handle.close()
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jsonl", type=Path, help="the test set: {audio, text} per line")
    p.add_argument("--url", help="the speech-recognition server")
    p.add_argument("--api-key-file", type=Path)
    p.add_argument("--model")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--worst", type=int, default=5)
    p.add_argument("--out", type=Path, help="save every clip here as it is done (and continue from it if it exists)")
    p.add_argument("--rescore", type=Path, help="score a file saved with --out again, without the server")
    args = p.parse_args()

    if args.rescore:
        results = read_results(args.rescore)[: args.limit or None]
    else:
        if not (args.jsonl and args.url):
            p.error("--jsonl and --url are needed unless --rescore is given")
        key = args.api_key_file.read_text().strip() if args.api_key_file else None
        rows = [json.loads(line) for line in args.jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = rows[: args.limit] if args.limit else rows
        results = score(rows, lambda audio: transcribe(args.url, Path(audio).read_bytes(), key, args.model), args.out,
                        lambda done, total: print(f"{done}/{total}", flush=True))
    summary = summarize(results, args.worst)
    worst = summary.pop("worst")
    print(json.dumps(summary, ensure_ascii=False))
    if args.out and not args.rescore:
        args.out.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in worst:
        print(f"\nWER {error_rates([(r['reference'], r['hypothesis'])])['wer']:.2f}\n  ref: {normalize(r['reference'])}\n  hyp: {normalize(r['hypothesis'])}")


if __name__ == "__main__":
    main()
