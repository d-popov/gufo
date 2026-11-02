# Qwen3-TTS fine-tuning, stoppable and resumable

A full fine-tune of `Qwen3-TTS-12Hz-*-Base` to one speaker, for `gufo serve tts`. The training step is
the upstream `finetuning/sft_12hz.py` (pinned in `setup.sh`); `train.py` adds stopping and resuming,
progress files and a progress viewer. Nothing here is part of Gufo's runtime.

```sh
# once: upstream scripts, qwen-tts and its pinned dependencies beside Unsloth Studio's ROCm PyTorch,
# and the 1.7B Base model (the upstream step fails on 0.6B: its text embedding is wider than its talker)
tools/audio/tts_finetune/setup.sh ~/qwen3-tts-train 1.7B
T=~/qwen3-tts-train

# data: JSONL of {"audio": wav, "text": transcript, "ref_audio": one fixed clip of the speaker}
$T/run.sh $T/Qwen3-TTS/finetuning/prepare_data.py --device cuda:0 \
  --tokenizer_model_path $T/Qwen3-TTS-Tokenizer-12Hz --input_jsonl train_raw.jsonl --output_jsonl train.jsonl

# train (stop the Gufo servers first: training needs the GPU memory)
$T/run.sh tools/audio/tts_finetune/train.py --init-model $T/Qwen3-TTS-12Hz-1.7B-Base \
  --qwen-finetuning-dir $T/Qwen3-TTS/finetuning --train-jsonl train.jsonl --output-dir runs/bg1 \
  --speaker-name bg_voice --epochs 10 --resume auto
```

In another console:

```sh
tools/audio/tts_finetune/status.py runs/bg1          # live view: epoch, step, loss, s/step, ETA, last checkpoint
tools/audio/tts_finetune/status.py runs/bg1 stop     # save and stop after the current step
tools/audio/tts_finetune/status.py runs/bg1 history  # the loss curve
```

**Stop and continue.** Ctrl-C, SIGTERM or `status.py ... stop` finishes the optimizer step in hand, saves
a checkpoint and exits; run the same `train.py` command again (`--resume auto`) and it continues at the
next step, with the same data order, optimizer state and random state. A second Ctrl-C quits without
saving. A checkpoint is also written every `--checkpoint-minutes` (default 30), so a crash or power cut loses
at most that long, and none at epoch boundaries. Writing the 22 GB of the 1.7B model took 13 s once and
65-77 s on later saves in a short trial (disk-bound, each save also deletes an old one), so 30 minutes
costs about 1-4% of the run; shorten it only for a flaky machine. `--max-steps N` stops after N optimizer steps,
for trials. `tests/tools/tts_finetune_test.py` checks, on a toy model, that stopping after any step and
resuming reaches exactly the weights of an uninterrupted run, with an epoch that does not divide into
accumulation groups.

**Sizes.** A resume checkpoint holds the weights and AdamW state: about 12x the parameter count in bytes
with `--weights fp32` (the default; 1.7B is roughly 20 GB, 0.6B roughly 7 GB), and `--keep` (default 2) of
them are kept. `final/` is the finished model in bf16 (about 4 GB for 1.7B), in the layout Gufo loads; with
`--export-every-epoch` each epoch also writes `checkpoint-epoch-N/`, and `--export-only --resume DIR`
exports a checkpoint on demand.

`--weights fp32` keeps small updates that a learning rate of 2e-6 would lose in bf16 weights (the
official script trains bf16 weights); compute is bf16 either way. `--attn sdpa` is the default because
`flash_attention_2` is not built for this GPU.

**Language IDs.** Qwen3-TTS prompts its talker either with "language auto" or with a language's codec token, and
`gufo serve tts` builds both. The upstream script trains every row as auto. With `--tagged-fraction F` the share `F` of
the rows whose `language` the model has an ID for (or that `--default-language` names) train with the ID, in the same
prefix the server builds (`language_layout.py`; a tagged row's text, codes, labels and speaker slot sit one position
later), and the rest train as auto, so the model answers both to a named language and to none. A language the model
lacks takes an unused talker codec token: `--language-id bulgarian=2100 --default-language bulgarian
--tagged-fraction 0.5`. The ID is written to the exported `config.json`, which is what makes the server accept
`"language": "bulgarian"`. Whether a new token learns a language well is not validated; listen to the held-out clips.

**A new language is not validated.** The upstream script handles one speaker and gives the model no
language tag for it. A community thread ([QwenLM/Qwen3-TTS#323](https://github.com/QwenLM/Qwen3-TTS/issues/323))
reports a Bengali fine-tune that kept a Hindi accent and suggests registering a codec language ID in
`config.json` and training with it; its author had not tried that, and the 20+ hours of data it advises is
that commenter's rule of thumb. Neither is tested here. Start with a small pilot (a few hours of clean
audio, one or two epochs, `--max-steps`) and listen before committing days of GPU time.
