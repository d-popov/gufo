#!/bin/sh
# One-time setup for fine-tuning Qwen3-ASR, in a directory of your choice (default ~/qwen3-asr-train):
#   - the upstream finetuning script at a pinned commit
#   - qwen-asr and its pinned dependencies, installed beside (not into) Unsloth Studio's Python, which
#     supplies the ROCm PyTorch. run.sh runs anything with both.
#   - optionally the base model: setup.sh [DIR] 1.7B|0.6B
set -eu
DIR=${1:-$HOME/qwen3-asr-train}
SIZE=${2:-}
PIN=9567667698f195fa807b1581de5c03184e63d2b0
PY=${STUDIO_PYTHON:-$HOME/.unsloth/studio/unsloth_studio/bin/python}
mkdir -p "$DIR"
if [ ! -d "$DIR/Qwen3-ASR" ]; then
  git clone -q https://github.com/QwenLM/Qwen3-ASR.git "$DIR/Qwen3-ASR"
fi
git -C "$DIR/Qwen3-ASR" checkout -q "$PIN"
"$PY" -m pip install -q --target "$DIR/deps" --no-deps "$DIR/Qwen3-ASR" transformers==4.57.6 accelerate==1.12.0 \
  "tokenizers>=0.22.0,<=0.23.0" "huggingface_hub>=0.34.0,<1.0" "datasets>=3.0,<4.1" librosa soynlp==0.0.493 \
  qwen-omni-utils sox pytz six dill multiprocess xxhash
# qwen-asr imports nagisa (a Japanese tokenizer needing the DyNet library) only for its forced aligner, which fine-tuning
# does not use; a stub lets the import succeed and fails loudly if the aligner is ever used.
mkdir -p "$DIR/deps"
cat > "$DIR/deps/nagisa.py" <<'STUB'
def __getattr__(name):
    raise RuntimeError("nagisa is stubbed out by setup.sh; the forced aligner needs the real package")
STUB
case "$SIZE" in
  "") ;;
  0.6B|1.7B) hf download "Qwen/Qwen3-ASR-$SIZE" --local-dir "$DIR/Qwen3-ASR-$SIZE" ;;
  *) echo "the model is 0.6B or 1.7B" >&2; exit 2 ;;
esac
cat > "$DIR/run.sh" <<RUN
#!/bin/sh
# Runs a command (default: python) with Studio's ROCm Python plus the qwen-asr dependencies.
export PYTHONPATH="$DIR/deps\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" "\$@"
RUN
chmod +x "$DIR/run.sh"
echo "ready: $DIR/run.sh $DIR/Qwen3-ASR/finetuning/qwen3_asr_sft.py --model_path $DIR/Qwen3-ASR-1.7B ..."
