# Fine-tuning Qwen3-ASR for another language

Qwen3-ASR hears Bulgarian as Macedonian: on 150 FLEURS Bulgarian test clips `gufo serve asr` labelled 147 of them
"macedonian" and transcribed them in Macedonian spelling (WER 58.7%, CER 19.8% against the Bulgarian reference; the rest
heard as Czech and Russian). This directory holds the tools to measure that and to train against it with the upstream
script (`QwenLM/Qwen3-ASR` `finetuning/qwen3_asr_sft.py`, fetched at a pinned commit, not vendored).

```sh
tools/audio/asr_finetune/setup.sh ~/qwen3-asr-train 1.7B       # upstream script, qwen-asr, base model, run.sh
# FLEURS (CC-BY-4.0) and Common Voice (CC0) downloads, then:
prepare_data.py fleurs --root fleurs/data/bg_bg --split train --out OUT --language Bulgarian
prepare_data.py commonvoice --root cv22 --lang bg --split dev --out OUT --language Bulgarian
wer.py --jsonl OUT/fleurs-test-plain.jsonl --url http://127.0.0.1:1235 --api-key-file KEYFILE   # baseline
```

`prepare_data.py` writes 16 kHz mono WAVs and `*.jsonl` with `language Bulgarian<asr_text>` before each reference (the
form the upstream script and the model use; the model writes the language it hears there, which is how `gufo serve asr`
reports it), plus `*-plain.jsonl` without the prefix for `wer.py`. `wer.py` works against any
`/v1/audio/transcriptions` server; WER and CER are summed over the corpus after lower-casing and removing punctuation.

Check the data's licence before publishing a model trained on it. FLEURS is CC-BY-4.0 (attribution required) and
Common Voice is CC0. A dataset card's claim of permission (for instance for audio taken from video sites) is not
something this tooling can verify.

`export.py --checkpoint OUT/checkpoint-N --base Qwen3-ASR-1.7B --out DIR` turns a checkpoint into a model directory
`gufo serve asr` loads: the trainer writes one `model.safetensors`, Gufo reads the released model's two shards, so the
tensors are re-split along the base model's index (the tied output head, which the trainer saves once, is written again).
Step 600 of 888 of the Bulgarian run scored WER 16.4% and CER 6.2% on the 150 FLEURS test clips the stock model scored
58.7% and 19.8% on.

Two things the upstream script does that cost time: it ignores `--save_steps` on a resumed run (the interval saved in the
checkpoint wins), and resuming with another micro-batch size ends the run at once with no steps done.
