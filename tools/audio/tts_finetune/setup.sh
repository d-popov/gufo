#!/bin/sh
# One-time setup for train.py, in a directory of your choice (default ~/qwen3-tts-train):
#   - the upstream finetuning scripts at a pinned commit (dataset.py, prepare_data.py)
#   - qwen-tts and its pinned dependencies, installed beside (not into) Unsloth Studio's Python,
#     which supplies the ROCm PyTorch. Use run.sh to run anything with both.
#   - optionally a Base model: setup.sh [DIR] 0.6B|1.7B
set -eu
DIR=${1:-$HOME/qwen3-tts-train}
SIZE=${2:-}
PIN=022e286b98fbec7e1e916cb940cdf532cd9f488e
PY=${STUDIO_PYTHON:-$HOME/.unsloth/studio/unsloth_studio/bin/python}
mkdir -p "$DIR"
if [ ! -d "$DIR/Qwen3-TTS" ]; then
  git clone -q https://github.com/QwenLM/Qwen3-TTS.git "$DIR/Qwen3-TTS"
fi
git -C "$DIR/Qwen3-TTS" checkout -q "$PIN"
# transformers and accelerate are pinned by qwen-tts; torch, torchaudio and friends come from Studio.
"$PY" -m pip install -q --target "$DIR/deps" --no-deps qwen-tts==0.1.1 transformers==4.57.3 accelerate==1.12.0 \
  "tokenizers>=0.22.0,<=0.23.0" "huggingface_hub>=0.34.0,<1.0" sox onnxruntime einops
case "$SIZE" in
  "") ;;
  0.6B|1.7B)
    hf download "Qwen/Qwen3-TTS-12Hz-$SIZE-Base" --local-dir "$DIR/Qwen3-TTS-12Hz-$SIZE-Base"
    hf download Qwen/Qwen3-TTS-Tokenizer-12Hz --local-dir "$DIR/Qwen3-TTS-Tokenizer-12Hz" ;;
  *) echo "the model is 0.6B or 1.7B" >&2; exit 2 ;;
esac
cat > "$DIR/run.sh" <<RUN
#!/bin/sh
# Runs a command (default: python) with Studio's ROCm Python plus the qwen-tts dependencies.
export PYTHONPATH="$DIR/deps\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" "\$@"
RUN
chmod +x "$DIR/run.sh"
echo "ready: $DIR/run.sh train.py --qwen-finetuning-dir $DIR/Qwen3-TTS/finetuning ..."
