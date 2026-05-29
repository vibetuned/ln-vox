#!/usr/bin/env bash
# Clone Dramabox and install its requirements into the active uv venv.
# Dramabox is not pip-installable; we vendor it at external/DramaBox.
#
# Platform handling:
#   - x86_64 Linux     → install Dramabox's requirements.txt verbatim (pins torch==2.8.0+cu128).
#   - aarch64 Linux (DGX Spark / Grace Hopper / Jetson)
#                      → install Dramabox's requirements MINUS torch/torchaudio
#                        (the pinned 2.8.0 has no CUDA-enabled aarch64 wheel),
#                        then pull torch>=2.10 from the cu130 index which DOES
#                        ship aarch64+sbsa builds with CUDA.
#   - Darwin (Apple Silicon, secondary target — see DESIGN.md §11)
#                      → install Dramabox's requirements MINUS torch/torchaudio
#                        AND minus bitsandbytes (CUDA-only, no macOS wheel);
#                        let pip pull the default MPS-capable torch wheel and
#                        verify torch.backends.mps instead of torch.cuda.
#                        DramaboxClient sets bnb_4bit=False on MPS, so the
#                        missing bitsandbytes doesn't break the warm Gemma load.
#
# The DramaboxClient wrapper monkey-patches torchaudio.save so version drift
# between Dramabox's expected 2.8 and the 2.10+ we install on aarch64 doesn't
# break the WAV writer.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXTERNAL="$ROOT/external/DramaBox"
ARCH="$(uname -m)"
OS="$(uname -s)"

if [ ! -d "$EXTERNAL" ]; then
    echo "==> Cloning Dramabox into $EXTERNAL"
    mkdir -p "$ROOT/external"
    git clone --depth 1 https://github.com/resemble-ai/DramaBox.git "$EXTERNAL"
else
    echo "==> Dramabox already cloned at $EXTERNAL"
    echo "    (pull latest with: git -C $EXTERNAL pull)"
fi

# Locate Dramabox's requirements file.
REQ_FILE=""
for f in requirements.txt requirements/requirements.txt requirements_inference.txt; do
    if [ -f "$EXTERNAL/$f" ]; then
        REQ_FILE="$EXTERNAL/$f"
        break
    fi
done
if [ -z "$REQ_FILE" ]; then
    echo "==> No requirements file found in $EXTERNAL — inspect the repo and install deps manually" >&2
    ls -la "$EXTERNAL" >&2
    exit 1
fi

cd "$ROOT"

# Darwin (Apple Silicon) must be matched BEFORE the arm64 leg of the case
# below — `uname -m` on M-series Macs also returns "arm64", and the existing
# branch assumes Linux+CUDA wheels exist on the cu130 index.
if [ "$OS" = "Darwin" ]; then
    echo "==> Darwin detected (Apple Silicon, secondary target)"
    echo "    Filtering torch* and bitsandbytes out of Dramabox's requirements"
    echo "    (bitsandbytes is CUDA-only; default PyPI torch is MPS-capable)."
    FILTERED="$(mktemp)"
    trap 'rm -f "$FILTERED"' EXIT
    grep -v -i -E '^[[:space:]]*(torch|torchaudio|torchvision|bitsandbytes)([[:space:]]|=|<|>|~|!|$)' \
        "$REQ_FILE" > "$FILTERED"
    echo "    Installing filtered Dramabox requirements…"
    uv pip install -r "$FILTERED"
    echo "    Installing torch + torchaudio from default PyPI (MPS-capable)…"
    uv pip install "torch>=2.10,<2.12" "torchaudio>=2.10,<2.12"
else
    case "$ARCH" in
        x86_64)
            echo "==> x86_64 → installing $REQ_FILE verbatim"
            uv pip install -r "$REQ_FILE"
            ;;

        aarch64|arm64)
            echo "==> aarch64 detected (DGX Spark / Grace Hopper / Jetson)"
            echo "    Filtering torch / torchaudio out of Dramabox's requirements"
            echo "    (no CUDA-enabled aarch64 wheel exists for torch 2.8.0)."
            FILTERED="$(mktemp)"
            trap 'rm -f "$FILTERED"' EXIT
            # Drop any line that pins torch / torchaudio / torchvision regardless
            # of the operator (==, >=, <=, ~=, !=).
            grep -v -i -E '^[[:space:]]*(torch|torchaudio|torchvision)([[:space:]]|=|<|>|~|!|$)' "$REQ_FILE" > "$FILTERED"
            echo "    Installing filtered Dramabox requirements…"
            uv pip install -r "$FILTERED"
            echo "    Installing torch + torchaudio from the cu130 aarch64+sbsa wheels…"
            uv pip install \
                --index-url https://download.pytorch.org/whl/cu130 \
                "torch>=2.10,<2.12" \
                "torchaudio>=2.10,<2.12"
            ;;

        *)
            echo "==> Unknown arch '$ARCH'. Trying Dramabox's requirements verbatim and praying." >&2
            uv pip install -r "$REQ_FILE"
            ;;
    esac
fi

echo ""
if [ "$OS" = "Darwin" ]; then
    echo "==> Verifying torch.backends.mps is usable…"
    if uv run python - <<'PY'
import sys
import torch
print(f"   torch={torch.__version__}  mps_available={torch.backends.mps.is_available()}")
if not torch.backends.mps.is_available():
    sys.exit(1)
PY
    then
        echo "==> ✓ Dramabox ready at $EXTERNAL (MPS backend)"
        echo "    First model run will auto-download weights from HuggingFace (~15 GB)."
        echo "    Expect 5-10x real-time render on Apple Silicon — see DESIGN.md §11.3."
    else
        echo "" >&2
        echo "==> ⚠  torch is installed but MPS is NOT available." >&2
        echo "    Are you running on Apple Silicon (M-series)? Intel Macs have no" >&2
        echo "    MPS backend. uv run python -c 'import torch; print(torch.__version__)'" >&2
        exit 1
    fi
else
    echo "==> Verifying torch.cuda is usable…"
    if uv run python - <<'PY'
import sys
import torch
print(f"   torch={torch.__version__}  cuda_available={torch.cuda.is_available()}")
if not torch.cuda.is_available():
    sys.exit(1)
print(f"   device={torch.cuda.get_device_name(0)}")
PY
    then
        echo "==> ✓ Dramabox ready at $EXTERNAL"
        echo "    First model run will auto-download weights from HuggingFace (~15 GB)."
    else
        echo "" >&2
        echo "==> ⚠  torch is installed but CUDA is NOT available." >&2
        echo "    On aarch64 this usually means the wheel index didn't have a" >&2
        echo "    CUDA-enabled build for the requested version. Try:" >&2
        echo "       uv pip install --force-reinstall \\" >&2
        echo "           --index-url https://download.pytorch.org/whl/cu130 \\" >&2
        echo "           torch torchaudio" >&2
        exit 1
    fi
fi
