#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MODEL_DIR=${MODEL_DIR:-/opt/encodec-live/models}
NATIVE_ENCODER=${NATIVE_ENCODER:-"$PROJECT_DIR/bin/encodec-live-native"}
PYTHON=${PYTHON:-python3}
PYTORCH_INDEX_URL=${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}
PYTORCH_PACKAGE=${PYTORCH_PACKAGE:-torch==2.4.1}
TORCH_HOME=${TORCH_HOME:-"$PROJECT_DIR/.cache/torch"}

mkdir -p "$MODEL_DIR" "$TORCH_HOME"

# The ECNG publisher model is encoder-only FP32 and is distributed with the
# project. Unlike official EnCodec exports, it is checkpoint-specific.
ECNG_SOURCE="$PROJECT_DIR/models/ecng-24khz-encoder-f32.bin"
ECNG_MODEL="$MODEL_DIR/ecng-24khz-encoder-f32.bin"
if [ ! -f "$ECNG_SOURCE" ]; then
    echo "ECNG model is missing from the project: $ECNG_SOURCE" >&2
    exit 1
fi
if ! cmp -s "$ECNG_SOURCE" "$ECNG_MODEL"; then
    install -m 0644 "$ECNG_SOURCE" "$ECNG_MODEL"
fi
"$NATIVE_ENCODER" --codec ECNG --model "$ECNG_MODEL" --samplerate 24 \
    --codebooks 4 --threads 1 --beam-size 1 --check-model

needs_export=false
for samplerate in 24 48; do
    model="$MODEL_DIR/encodec_${samplerate}khz-combined-f32.bin"
    codebooks=4
    [ "$samplerate" -eq 48 ] && codebooks=2
    if [ ! -f "$model" ] || ! "$NATIVE_ENCODER" \
        --model "$model" --samplerate "$samplerate" --codebooks "$codebooks" \
        --threads 1 --check-model >/dev/null 2>&1; then
        needs_export=true
    fi
done

if [ "$needs_export" = false ]; then
    echo "Combined 24 kHz and 48 kHz models are already valid in $MODEL_DIR"
    exit 0
fi

EXPORT_ENV=$(mktemp -d "${TMPDIR:-/tmp}/encodec-model-export.XXXXXX")
cleanup() {
    rm -rf -- "$EXPORT_ENV"
}
trap cleanup EXIT HUP INT TERM

"$PYTHON" -m venv "$EXPORT_ENV/venv"
"$EXPORT_ENV/venv/bin/python" -m pip install --upgrade pip
"$EXPORT_ENV/venv/bin/python" -m pip install 'numpy<2'
"$EXPORT_ENV/venv/bin/python" -m pip install "$PYTORCH_PACKAGE" --index-url "$PYTORCH_INDEX_URL"

for samplerate in 24 48; do
    model="$MODEL_DIR/encodec_${samplerate}khz-combined-f32.bin"
    temporary="$EXPORT_ENV/encodec_${samplerate}khz-combined-f32.bin"
    codebooks=4
    [ "$samplerate" -eq 48 ] && codebooks=2
    if [ -f "$model" ] && "$NATIVE_ENCODER" \
        --model "$model" --samplerate "$samplerate" --codebooks "$codebooks" \
        --threads 1 --check-model >/dev/null 2>&1; then
        echo "Keeping valid model: $model"
        continue
    fi
    TORCH_HOME="$TORCH_HOME" "$EXPORT_ENV/venv/bin/python" \
        "$PROJECT_DIR/tools/export_cpp_models.py" \
        --sample-rate "$((samplerate * 1000))" \
        --include-encoder \
        --output "$temporary"
    install -m 0644 "$temporary" "$model"
    "$NATIVE_ENCODER" --model "$model" --samplerate "$samplerate" \
        --codebooks "$codebooks" --threads 1 --check-model
done

echo "Prepared combined native models in $MODEL_DIR"
