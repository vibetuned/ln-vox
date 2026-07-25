#!/usr/bin/env bash
# Clone the VibeVoice community fork and pip-install it (editable) into the
# active uv venv; optionally download the VibeVoice-Large weights (~18 GB).
# See DESIGN.md §16.6.
#
# Microsoft pulled the official inference code, so we vendor the community
# fork (MIT) at external/VibeVoice, pinned to a known-good commit. Unlike
# Dramabox it IS a real package — no sys.path injection, just an editable
# install. Its deps (transformers>=4.51, diffusers, librosa, accelerate)
# ride on whatever torch the platform installer already put in the venv
# (2.8 x86_64 / 2.10 aarch64 / >=2.10 Darwin — all sufficient).
#
# Usage:
#   ./scripts/setup_vibevoice.sh                # clone + install only
#   ./scripts/setup_vibevoice.sh --weights      # + weights from ModelScope
#   ./scripts/setup_vibevoice.sh --weights-hf   # + weights from Hugging Face
#
# Weights land in models/VibeVoice-Large/. At run time the client resolves
# the model source as: $LNVOX_VIBEVOICE_MODEL → models/VibeVoice-Large/ →
# the HF repo id 'microsoft/VibeVoice-Large' (auto-downloads to the HF cache).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXTERNAL="$ROOT/external/VibeVoice"
REPO_URL="https://github.com/vibevoice-community/VibeVoice.git"
# main as of 2026-06-12; override with LNVOX_VIBEVOICE_REF=<sha|branch>.
REF="${LNVOX_VIBEVOICE_REF:-07cb79feadd2d3fd7f47530d4c964a12857936a0}"
MODELS_DIR="$ROOT/models/VibeVoice-Large"

WEIGHTS=""
for arg in "$@"; do
    case "$arg" in
        --weights)    WEIGHTS="modelscope" ;;
        --weights-hf) WEIGHTS="hf" ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ ! -d "$EXTERNAL" ]; then
    echo "==> Cloning VibeVoice (community fork) into $EXTERNAL"
    mkdir -p "$ROOT/external"
    git clone "$REPO_URL" "$EXTERNAL"
else
    echo "==> VibeVoice already cloned at $EXTERNAL"
fi

echo "==> Checking out pinned ref $REF"
git -C "$EXTERNAL" fetch --quiet origin || true
git -C "$EXTERNAL" checkout --quiet "$REF"

echo "==> Installing vibevoice (editable) into the uv venv"
uv pip install -e "$EXTERNAL"

echo "==> Verifying import"
uv run python -c "import vibevoice; print('    vibevoice OK:', vibevoice.__file__)"

if [ -n "$WEIGHTS" ]; then
    if [ -f "$MODELS_DIR/config.json" ]; then
        echo "==> Weights already present at $MODELS_DIR — skipping download"
    elif [ "$WEIGHTS" = "modelscope" ]; then
        echo "==> Downloading VibeVoice-Large weights from ModelScope (~18 GB)"
        mkdir -p "$MODELS_DIR"
        uvx modelscope download --model microsoft/VibeVoice-Large --local_dir "$MODELS_DIR"
    else
        echo "==> Downloading VibeVoice-Large weights from Hugging Face (~18 GB)"
        mkdir -p "$MODELS_DIR"
        # `hf` is the current CLI name; fall back to the deprecated one.
        uv run hf download microsoft/VibeVoice-Large --local-dir "$MODELS_DIR" \
            || uv run huggingface-cli download microsoft/VibeVoice-Large --local-dir "$MODELS_DIR"
    fi
else
    echo ""
    echo "NOTE: weights not downloaded (pass --weights for ModelScope, --weights-hf for HF)."
    echo "      Without a local copy the client falls back to the HF repo id"
    echo "      'microsoft/VibeVoice-Large' and downloads to the HF cache on first run."
fi

echo "==> Done. Render with: uv run lnvox s4 <book> --tts-backend vibevoice"
