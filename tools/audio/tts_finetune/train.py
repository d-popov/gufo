#!/usr/bin/env python3
# coding=utf-8
# Adapted from QwenLM/Qwen3-TTS finetuning/sft_12hz.py (Apache-2.0, Copyright 2026 The Alibaba
# Qwen team), pinned in setup.sh. The training step is theirs; what is added is stopping and
# resuming (full state checkpoints, deterministic data order), progress files for status.py,
# float32 master weights, and a step budget.
"""Resumable full fine-tune of Qwen3-TTS-12Hz Base to one speaker.

Stop it any time with Ctrl-C, SIGTERM or `status.py OUTPUT_DIR stop`: it finishes the optimizer step
in hand, saves a checkpoint (weights, optimizer, data position, random state) and exits. Start it
again with --resume auto to continue from there, at most one optimizer step behind. A second Ctrl-C
exits at once without saving. Checkpoints are also written every --checkpoint-minutes and at every
epoch's end, so a crash loses at most that long.

OUTPUT_DIR/
  state.json       what the run is doing now (read by status.py)
  progress.jsonl   one line per --log-every optimizer steps
  ckpt/step-N/     resume checkpoints (the newest --keep); ckpt/LATEST names the newest
  final/           the finished model in the layout `gufo serve tts` loads (written at the end)
"""

import argparse
import json
import math
import os
import shutil
import signal
import sys
import time
from pathlib import Path

import torch
from accelerate import Accelerator, skip_first_batches
from safetensors.torch import save_file
from torch.optim import AdamW
from torch.utils.data import DataLoader
from transformers import AutoConfig

SPEAKER_ROW = 3000  # the codec-embedding row the official script gives the new speaker


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--init-model", type=Path, required=True, help="local directory of Qwen3-TTS-12Hz-*-Base")
    p.add_argument("--train-jsonl", type=Path, required=True, help="prepare_data.py output (audio_codes included)")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--qwen-finetuning-dir", type=Path, required=True,
                   help="the upstream finetuning/ directory setup.sh checks out (for dataset.py)")
    p.add_argument("--speaker-name", default="speaker")
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--grad-accum", type=int, default=4)
    p.add_argument("--lr", type=float, default=2e-6)
    p.add_argument("--warmup-steps", type=int, default=0, help="optimizer steps of linear warm-up")
    p.add_argument("--epochs", type=int, default=10)
    p.add_argument("--max-steps", type=int, default=0, help="stop (and save) after this many optimizer steps; 0 = no limit")
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--attn", default="sdpa", choices=["sdpa", "eager", "flash_attention_2"])
    p.add_argument("--weights", default="fp32", choices=["fp32", "bf16"],
                   help="dtype of the weights being trained; fp32 keeps small updates (bf16 compute either way)")
    p.add_argument("--log-every", type=int, default=10, help="optimizer steps between progress lines")
    p.add_argument("--checkpoint-minutes", type=float, default=30.0)
    p.add_argument("--keep", type=int, default=2, help="resume checkpoints kept (each holds weights and optimizer)")
    p.add_argument("--resume", default="none", help="'auto' (newest in OUTPUT_DIR/ckpt), a checkpoint directory, or 'none'")
    p.add_argument("--export-every-epoch", action="store_true", help="also write checkpoint-epoch-N/ in the final layout")
    p.add_argument("--export-only", action="store_true", help="write final/ from --resume and exit")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------- files

def write_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


class Run:
    """The run's files: state.json for status.py, progress.jsonl, and the stop request."""

    def __init__(self, out: Path, args):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        (out / "ckpt").mkdir(exist_ok=True)
        self.state = {"status": "starting", "pid": os.getpid(), "started": time.time(),
                      "args": {k: str(v) for k, v in vars(args).items()}}
        self.stop_file = out / "STOP"
        self.stop_file.unlink(missing_ok=True)  # a request left from before the last run
        self.flag = False

    def update(self, **fields) -> None:
        self.state.update(fields, updated=time.time())
        write_json(self.out / "state.json", self.state)

    def log(self, **fields) -> None:
        with open(self.out / "progress.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), **fields}) + "\n")

    def stop_requested(self) -> bool:
        return self.flag or self.stop_file.exists()

    def install_signals(self) -> None:
        def handler(signum, frame):
            if self.flag:
                raise KeyboardInterrupt  # the second request: leave now, nothing saved
            self.flag = True
            print(f"\n[{signal.Signals(signum).name}] finishing this step, saving, stopping "
                  "(again to quit without saving)", flush=True)
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)


# ---------------------------------------------------------------------------- checkpoints

def save_checkpoint(accelerator, run: Run, keep: int, progress: dict, speaker) -> Path:
    """OUTPUT/ckpt/step-N, written under a temporary name and renamed so a crash never leaves half."""
    ckpt = run.out / "ckpt"
    name = f"step-{progress['global_step']}"
    tmp = ckpt / f"tmp-{name}"
    shutil.rmtree(tmp, ignore_errors=True)
    run.update(status="saving")
    accelerator.save_state(str(tmp))
    write_json(tmp / "trainer.json", progress)
    if speaker is not None:
        torch.save(speaker.cpu(), tmp / "speaker.pt")
    final = ckpt / name
    shutil.rmtree(final, ignore_errors=True)
    os.replace(tmp, final)
    (ckpt / "LATEST").write_text(name)
    kept = sorted((d for d in ckpt.glob("step-*")), key=lambda d: int(d.name.split("-")[1]))
    for old in kept[:-keep] if keep > 0 else []:
        shutil.rmtree(old, ignore_errors=True)
    run.update(status="running", last_checkpoint=name, last_checkpoint_at=time.time())
    return final


def find_resume(spec: str, out: Path) -> Path | None:
    if spec == "none":
        return None
    if spec == "auto":
        latest = out / "ckpt" / "LATEST"
        return out / "ckpt" / latest.read_text().strip() if latest.exists() else None
    return Path(spec)


def export_model(model_state: dict, speaker, args, dest: Path) -> None:
    """The official layout: the base model's files, config switched to custom_voice with the new
    speaker, and model.safetensors in bf16 without the speaker encoder."""
    init = args.init_model
    shutil.copytree(init, dest, dirs_exist_ok=True)
    config = json.loads((init / "config.json").read_text(encoding="utf-8"))
    config["tts_model_type"] = "custom_voice"
    talker = config.get("talker_config", {})
    talker["spk_id"] = {args.speaker_name: SPEAKER_ROW}
    talker["spk_is_dialect"] = {args.speaker_name: False}
    config["talker_config"] = talker
    (dest / "config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    state = {k: v.detach().to("cpu", torch.bfloat16) for k, v in model_state.items()
             if not k.startswith("speaker_encoder")}
    weight = state["talker.model.codec_embedding.weight"]
    weight[SPEAKER_ROW] = speaker[0].detach().to(weight.device).to(weight.dtype)
    save_file(state, str(dest / "model.safetensors"))


# ---------------------------------------------------------------------------- training

def train_loop(accelerator, model, optimizer, dataset, collate, step_fn, args, run: Run, progress: dict, speaker,
               export_epoch=None):
    """Train from `progress` (epoch, step_in_epoch, ...) until the epochs are done, a stop is
    requested or --max-steps is reached. Returns ("stopped" | "done", speaker).

    The accumulation groups are counted in micro-batches from the start of each epoch, the last
    group of an epoch holding the remainder, so an epoch always ends on an optimizer step and a
    resume position is exact: epoch e, step s means micro-batches [0, s * grad_accum) of epoch e
    are done, in an order fixed by the seed and the epoch."""
    micro_per_epoch = math.ceil(len(dataset) / args.batch_size)
    steps_per_epoch = math.ceil(micro_per_epoch / args.grad_accum)
    total_steps = steps_per_epoch * args.epochs
    run.update(status="running", dataset_rows=len(dataset), steps_per_epoch=steps_per_epoch, total_steps=total_steps,
               epochs=args.epochs, batch_size=args.batch_size, grad_accum=args.grad_accum, **progress)

    def fresh_window() -> dict:
        return {"loss": 0.0, "micro": 0, "samples": 0, "since": time.monotonic(), "steps": 0}

    window = fresh_window()
    last_checkpoint = time.monotonic()

    for epoch in range(progress["epoch"], args.epochs):
        # The same shuffle every time this epoch is built, so a resume skips what was done.
        generator = torch.Generator().manual_seed(args.seed + epoch)
        loader = DataLoader(dataset, batch_size=args.batch_size, shuffle=True, collate_fn=collate, generator=generator)
        done = progress["step_in_epoch"] * args.grad_accum if epoch == progress["epoch"] else 0
        if done:
            loader = skip_first_batches(loader, done)
        progress["epoch"] = epoch
        for micro, batch in enumerate(loader, start=done):
            batch = {k: v.to(accelerator.device) if torch.is_tensor(v) else v for k, v in batch.items()}
            first = micro // args.grad_accum * args.grad_accum
            group = min(args.grad_accum, micro_per_epoch - first)
            lr = args.lr * (min(1.0, (progress["global_step"] + 1) / args.warmup_steps) if args.warmup_steps else 1.0)
            for param_group in optimizer.param_groups:
                param_group["lr"] = lr
            loss, embedding = step_fn(model, batch)
            if speaker is None:
                speaker = embedding
            accelerator.backward(loss / group)
            size = len(next(v for v in batch.values() if torch.is_tensor(v)))
            window["loss"] += float(loss.detach())
            window["micro"] += 1
            window["samples"] += size
            progress["samples_seen"] += size
            if micro + 1 < first + group:
                continue  # the group is not complete
            accelerator.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            optimizer.zero_grad()
            progress["global_step"] += 1
            progress["step_in_epoch"] += 1
            window["steps"] += 1
            if progress["global_step"] % args.log_every == 0 or progress["step_in_epoch"] == steps_per_epoch:
                took = time.monotonic() - window["since"]
                progress["train_seconds"] += took
                mean = window["loss"] / max(window["micro"], 1)
                rate = window["samples"] / max(took, 1e-9)
                per_step = took / max(window["steps"], 1)
                run.log(epoch=epoch, step=progress["step_in_epoch"], global_step=progress["global_step"],
                        steps_per_epoch=steps_per_epoch, total_steps=total_steps, lr=lr, loss=mean,
                        samples_per_s=rate, seconds_per_step=per_step)
                run.update(**progress, loss=mean, lr=lr, samples_per_s=rate, seconds_per_step=per_step)
                print(f"epoch {epoch} step {progress['step_in_epoch']}/{steps_per_epoch} "
                      f"(global {progress['global_step']}/{total_steps}) loss {mean:.4f} {rate:.2f} samples/s", flush=True)
                window = fresh_window()
            if run.stop_requested() or (args.max_steps and progress["global_step"] >= args.max_steps):
                save_checkpoint(accelerator, run, args.keep, dict(progress), speaker)
                return "stopped", speaker
            if time.monotonic() - last_checkpoint >= args.checkpoint_minutes * 60:
                save_checkpoint(accelerator, run, args.keep, dict(progress), speaker)
                last_checkpoint = time.monotonic()
        # The epoch is complete (a resume at its very end lands here with nothing left to do).
        progress["epoch"] = epoch + 1
        progress["step_in_epoch"] = 0
        save_checkpoint(accelerator, run, args.keep, dict(progress), speaker)
        last_checkpoint = time.monotonic()
        if export_epoch is not None:
            export_epoch(epoch, speaker)
    return "done", speaker


def main() -> int:
    args = parse_args()
    sys.path.insert(0, str(args.qwen_finetuning_dir))
    from dataset import TTSDataset  # upstream, pinned by setup.sh
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

    run = Run(args.output_dir, args)
    run.install_signals()
    torch.manual_seed(args.seed)

    accelerator = Accelerator(mixed_precision="bf16")
    dtype = torch.float32 if args.weights == "fp32" else torch.bfloat16
    qwen = Qwen3TTSModel.from_pretrained(str(args.init_model), torch_dtype=dtype, attn_implementation=args.attn)
    config = AutoConfig.from_pretrained(str(args.init_model))
    rows = [json.loads(line) for line in args.train_jsonl.read_text().splitlines() if line.strip()]
    dataset = TTSDataset(rows, qwen.processor, config)
    optimizer = AdamW(qwen.model.parameters(), lr=args.lr, weight_decay=0.01)
    model, optimizer = accelerator.prepare(qwen.model, optimizer)
    model.train()

    progress = {"epoch": 0, "step_in_epoch": 0, "global_step": 0, "samples_seen": 0, "train_seconds": 0.0}
    speaker = None
    resume = find_resume(args.resume, args.output_dir)
    if resume is not None:
        accelerator.load_state(str(resume))
        progress.update(json.loads((resume / "trainer.json").read_text()))
        speaker_file = resume / "speaker.pt"
        speaker = torch.load(speaker_file).to(accelerator.device) if speaker_file.exists() else None
        print(f"resumed from {resume.name}: epoch {progress['epoch']}, step {progress['step_in_epoch']}", flush=True)
    elif args.resume not in ("none", "auto"):
        sys.exit(f"no checkpoint at {args.resume}")

    def export(dest: Path) -> None:
        export_model(accelerator.unwrap_model(model).state_dict(), speaker, args, dest)

    if args.export_only:
        if resume is None or speaker is None:
            sys.exit("--export-only needs --resume pointing at a checkpoint")
        export(args.output_dir / "final")
        print(f"wrote {args.output_dir / 'final'}")
        return 0

    def export_epoch(epoch: int, current) -> None:
        nonlocal speaker
        speaker = current
        if args.export_every_epoch:
            export(args.output_dir / f"checkpoint-epoch-{epoch}")

    outcome, speaker = train_loop(accelerator, model, optimizer, dataset, dataset.collate_fn,
                                  lambda m, b: step(m, b, accelerator), args, run, progress, speaker, export_epoch)
    if outcome == "stopped":
        run.update(status="stopped", **progress)
        print(f"stopped at epoch {progress['epoch']} step {progress['step_in_epoch']} (global {progress['global_step']}); "
              "saved. Continue with --resume auto", flush=True)
        return 0
    export(args.output_dir / "final")
    run.update(status="done", **progress)
    print(f"done: {args.output_dir / 'final'}", flush=True)
    return 0


def step(model, batch, accelerator):
    """One micro-batch: the official sft_12hz.py computation, under bf16 autocast (the official run
    holds bf16 weights; with float32 master weights the compute still runs in bf16). Returns the
    loss and the speaker embedding of this batch's reference audio."""
    input_ids = batch["input_ids"]
    codec_ids = batch["codec_ids"]
    ref_mels = batch["ref_mels"]
    text_embedding_mask = batch["text_embedding_mask"]
    codec_embedding_mask = batch["codec_embedding_mask"]
    attention_mask = batch["attention_mask"]
    codec_0_labels = batch["codec_0_labels"]
    codec_mask = batch["codec_mask"]

    with accelerator.autocast():
        speaker_embedding = model.speaker_encoder(ref_mels.to(model.device).to(model.dtype)).detach()
        input_text_ids = input_ids[:, :, 0]
        input_codec_ids = input_ids[:, :, 1]
        input_text_embedding = model.talker.model.text_embedding(input_text_ids) * text_embedding_mask
        input_codec_embedding = model.talker.model.codec_embedding(input_codec_ids) * codec_embedding_mask
        input_codec_embedding[:, 6, :] = speaker_embedding
        input_embeddings = input_text_embedding + input_codec_embedding
        for i in range(1, 16):
            codec_i_embedding = model.talker.code_predictor.get_input_embeddings()[i - 1](codec_ids[:, :, i])
            input_embeddings = input_embeddings + codec_i_embedding * codec_mask.unsqueeze(-1)

        outputs = model.talker(inputs_embeds=input_embeddings[:, :-1, :], attention_mask=attention_mask[:, :-1],
                               labels=codec_0_labels[:, 1:], output_hidden_states=True)
        hidden_states = outputs.hidden_states[0][-1]
        talker_hidden_states = hidden_states[codec_mask[:, :-1]]
        talker_codec_ids = codec_ids[codec_mask]
        _, sub_talker_loss = model.talker.forward_sub_talker_finetune(talker_codec_ids, talker_hidden_states)
        loss = outputs.loss + 0.3 * sub_talker_loss
    return loss, speaker_embedding


if __name__ == "__main__":
    raise SystemExit(main())
