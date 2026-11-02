#!/usr/bin/env python3
"""Turn a fine-tuning checkpoint into a model directory `gufo serve asr` loads.

    export.py --checkpoint OUT/checkpoint-600 --base Qwen3-ASR-1.7B --out Qwen3-ASR-1.7B-bg

The upstream trainer writes one model.safetensors; Gufo reads the layout of the released model, tensors in the
shards its model.safetensors.index.json names. This writes the checkpoint's tensors into those shards (same
names, same dtype) and copies the base model's small files (config, tokenizer, index). It refuses a checkpoint
whose tensor names differ from the base model's.
"""

import argparse
import json
import shutil
from pathlib import Path

from safetensors import safe_open
from safetensors.torch import save_file


TIED = {"thinker.lm_head.weight": "thinker.model.embed_tokens.weight"}  # saved once when the embeddings are tied


def split_by_index(names, weight_map: dict) -> dict:
    """{shard file: [tensor names]} for the names the index assigns; raises if the sets differ. A tied output
    head is written from the embedding it shares, so it counts as present when that embedding is."""
    names = list(names) + [head for head, source in TIED.items() if source in names and head not in names]
    missing = sorted(set(weight_map) - set(names))
    extra = sorted(set(names) - set(weight_map))
    if missing or extra:
        raise SystemExit(f"tensor names differ from the base model: {len(missing)} missing (e.g. {missing[:2]}), "
                         f"{len(extra)} unexpected (e.g. {extra[:2]})")
    shards: dict = {}
    for name in names:
        shards.setdefault(weight_map[name], []).append(name)
    return shards


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    weight_map = json.loads((args.base / "model.safetensors.index.json").read_text())["weight_map"]
    args.out.mkdir(parents=True, exist_ok=True)
    with safe_open(args.checkpoint / "model.safetensors", framework="pt") as f:
        shards = split_by_index(list(f.keys()), weight_map)
        for shard, names in shards.items():
            have = set(f.keys())
            tensors = {name: f.get_tensor(name if name in have else TIED[name]) for name in names}
            for name in names:
                if name not in have:
                    tensors[name] = tensors[name].clone()  # safetensors refuses two names for one buffer
            save_file(tensors, args.out / shard, metadata={"format": "pt"})
    for path in args.base.iterdir():
        if path.is_file() and not path.name.endswith(".safetensors"):
            shutil.copy2(path, args.out / path.name)
    print(f"wrote {args.out}: {', '.join(sorted(shards))}")


if __name__ == "__main__":
    main()
