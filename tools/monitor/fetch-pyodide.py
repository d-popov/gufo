#!/usr/bin/env python3
"""Download the Pyodide runtime (CPython in WebAssembly) for the chat's Python tool.

Files go to static/pyodide/ next to this script and are served by gufo-monitor to signed-in
chat users only; nothing is loaded from another host while chatting. The release is pinned:
the core files by the SHA-256 sums below, the packages by the sums in the release's own lock
file. A file that does not match is deleted, never used. Run it once, and again to add
packages or after changing VERSION:

    python3 tools/monitor/fetch-pyodide.py [PACKAGE ...]

Default packages: numpy, pandas, matplotlib (with what they need), about 30 MB in all.
Python 3.10+, standard library only.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "314.0.7"
BASE = f"https://cdn.jsdelivr.net/pyodide/v{VERSION}/full/"
TARGET = Path(__file__).resolve().parent / "static" / "pyodide"
CORE = {
    "pyodide.mjs": "6f1d60f7bf529beb300f0f47983c921d3982363640ba20af0e38efdddbc66109",
    "pyodide.asm.mjs": "f7cdc8ece80678ceb712f8e65ebe6d3a83203a180c399865f49612a051693635",
    "pyodide.asm.wasm": "cc36e3cab04fdfc9a63ff13eb52eae2b911bf46c025cc7b281f394bd3de1d5e6",
    "python_stdlib.zip": "fa1957e5777068fc4f7437f96d860ae2fbe9c19732ba06c84e004ec16dd7dd7a",
    "pyodide-lock.json": "5dc2fc119108bc148c7457dc86e7675b5c87e1cafd420b9c34c1eaef7b36c010",
}
DEFAULT_PACKAGES = ("numpy", "pandas", "matplotlib")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(name: str, expected: str) -> int:
    """Make TARGET/name the file with this SHA-256; returns the bytes downloaded (0: kept)."""
    path = TARGET / name
    if path.is_file() and sha256(path) == expected:
        return 0
    partial = path.with_name(path.name + ".part")
    try:
        with urllib.request.urlopen(BASE + name, timeout=60) as response, partial.open("wb") as out:
            while chunk := response.read(1 << 20):
                out.write(chunk)
        if sha256(partial) != expected:
            raise SystemExit(f"{name}: checksum does not match the pinned release; not installed")
        partial.replace(path)
        return path.stat().st_size
    except urllib.error.URLError as error:
        raise SystemExit(f"{name}: download failed: {error}") from None
    finally:
        partial.unlink(missing_ok=True)


def closure(packages: dict, wanted: tuple[str, ...]) -> list[str]:
    """The wanted packages and everything they depend on, in the lock file's names."""
    seen: list[str] = []

    def walk(name: str):
        key = name.lower().replace("_", "-")
        if key not in packages:
            raise SystemExit(f"{name}: not in Pyodide {VERSION}")
        if key not in seen:
            seen.append(key)
            for dependency in packages[key].get("depends", []):
                walk(dependency)

    for name in wanted:
        walk(name)
    return seen


def main(argv: list[str]) -> int:
    wanted = tuple(argv) or DEFAULT_PACKAGES
    TARGET.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for name, digest in CORE.items():
        downloaded += fetch(name, digest)
    packages = json.loads((TARGET / "pyodide-lock.json").read_text())["packages"]
    names = closure(packages, wanted)
    for key in names:
        entry = packages[key]
        downloaded += fetch(entry["file_name"], entry["sha256"])
    print(f"Pyodide {VERSION}: {len(CORE)} core files and {len(names)} packages "
          f"({', '.join(names)}) in {TARGET}; downloaded {downloaded / 1e6:.1f} MB.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
