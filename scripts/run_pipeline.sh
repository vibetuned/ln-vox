#!/usr/bin/env bash
# Run the full ln-vox pipeline for one volume of a series.
#
# Usage:
#     ./scripts/run_pipeline.sh <series>/<volume-XX> [options]
#
# Options:
#     --narrator-clip <id>     Voicebank clip id for the Narrator. Optional when
#                              a previous volume of the same series already has
#                              one assigned — it'll be inherited automatically.
#     --book-title <title>     Final m4b title (default: book_id).
#     --novels-root <path>     Where the chapter .txt files live (default: novels).
#     --vllm-url <url>         Use an already-running vLLM at this URL (skip
#                              auto start/stop). Default: launch vLLM ourselves.
#     --llm-model <id>         HF model id for vLLM (default: google/gemma-4-E4B-it).
#                              Try `nvidia/Gemma-4-31B-IT-NVFP4` for better quality.
#     --max-model-len <N>      Override vLLM context window (default: model-dependent).
#     --skip-llm               Skip s1..s3 + voice cast (assume already done).
#     --skip-tts               Skip s4 (assume already rendered).
#     --skip-mix               Skip s5 (skip the m4b assembly).
#     --max-retries N          Auto-retry budget for s4 (default 30).
#
# vLLM lifecycle: by default the launcher starts vLLM in the background,
# waits for it to be ready, runs the LLM-phase stages, then stops it
# (freeing GPU memory for Dramabox). Pass --vllm-url to skip this if you
# already have a vLLM server elsewhere (or running externally on a second
# GPU). The full pipeline is non-interactive; safe for overnight runs.

set -uo pipefail

BOOK_ID=""
NARRATOR_CLIP=""
BOOK_TITLE=""
NOVELS_ROOT="novels"
VLLM_URL=""
LLM_MODEL=""
MAX_MODEL_LEN=""
SKIP_LLM=0
SKIP_TTS=0
SKIP_MIX=0
MAX_RETRIES="${MAX_RETRIES:-30}"

VLLM_PID=""
VLLM_LOG=""

usage() {
    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage 0 ;;
        --narrator-clip) NARRATOR_CLIP="$2"; shift 2 ;;
        --book-title)    BOOK_TITLE="$2";   shift 2 ;;
        --novels-root)   NOVELS_ROOT="$2";  shift 2 ;;
        --vllm-url)      VLLM_URL="$2";     shift 2 ;;
        --llm-model)     LLM_MODEL="$2";    shift 2 ;;
        --max-model-len) MAX_MODEL_LEN="$2"; shift 2 ;;
        --skip-llm)      SKIP_LLM=1;        shift   ;;
        --skip-tts)      SKIP_TTS=1;        shift   ;;
        --skip-mix)      SKIP_MIX=1;        shift   ;;
        --max-retries)   MAX_RETRIES="$2";  shift 2 ;;
        -*)              echo "Unknown option: $1" >&2; usage 2 ;;
        *)               if [ -z "$BOOK_ID" ]; then BOOK_ID="$1"; shift; else echo "Extra positional: $1" >&2; usage 2; fi ;;
    esac
done

if [ -z "$BOOK_ID" ]; then
    echo "ERROR: book id (e.g. 'toaru/volume-01') is required" >&2
    usage 2
fi

BOOK_TITLE="${BOOK_TITLE:-$BOOK_ID}"
NOVEL_DIR="$NOVELS_ROOT/$BOOK_ID"
BOOK_ART="artifacts/$BOOK_ID"

if [ ! -d "$NOVEL_DIR" ] && [ "$SKIP_LLM" -eq 0 ]; then
    echo "ERROR: novel dir not found: $NOVEL_DIR" >&2
    exit 1
fi

# Pin the lnvox CLI client to the right vLLM URL.
if [ -n "$VLLM_URL" ]; then
    export LNVOX_LLM__ENDPOINT="$VLLM_URL"
fi
LNVOX_VLLM_BASE="${LNVOX_LLM__ENDPOINT:-http://localhost:8000/v1}"

# Propagate model + max-model-len to both serve_vllm.sh and the lnvox client.
if [ -n "$LLM_MODEL" ]; then
    export LNVOX_LLM_MODEL="$LLM_MODEL"
    echo "Using LLM model: $LLM_MODEL"
fi
if [ -n "$MAX_MODEL_LEN" ]; then
    export LNVOX_LLM_MAX_LEN="$MAX_MODEL_LEN"
fi

banner() {
    echo ""
    echo "############################################################"
    echo "## $1"
    echo "############################################################"
}

# ----- Dependency phase management -------------------------------------------
#
# vLLM and Dramabox require incompatible torch / torchaudio versions:
#   vLLM>=0.19   → torchaudio>=2.10
#   Dramabox     → torch==2.8.0 / torchaudio==2.8.0 (per its requirements.txt)
# We swap the venv state between phases. uv is fast for no-op syncs, so the
# overhead is minimal when the state is already correct.

prepare_llm_env() {
    banner "Preparing venv for LLM phase (vLLM-compatible torch)"
    # If torch can't even import (libcudnn / NCCL ABI mismatch after a prior
    # botched install), uv sync won't rescue us because the lockfile thinks
    # everything is already installed. Nuke the venv and let uv rebuild.
    if [ -d .venv ] && ! uv run python -c "import torch, vllm" >/dev/null 2>&1; then
        echo "Detected broken venv (torch/vllm import fails). Recreating from scratch…"
        rm -rf .venv
    fi
    uv sync --extra serve --extra voice --extra tts
}

# Install Dramabox's requirements.txt with platform-aware torch handling.
# Dramabox pins torch==2.8.0 which has no CUDA-enabled aarch64 wheel — on that
# arch we strip the torch lines and pull torch>=2.10 from the cu130 index
# (which DOES ship aarch64+sbsa CUDA wheels). x86_64 keeps the verbatim pin.
# This mirrors scripts/setup_dramabox.sh so the venv stays consistent whether
# the user runs setup_dramabox.sh once or relies on prepare_tts_env to do it.
install_dramabox_reqs() {
    local req_file="external/DramaBox/requirements.txt"
    if [ ! -f "$req_file" ]; then
        echo "WARN: $req_file not found; run ./scripts/setup_dramabox.sh first" >&2
        return 1
    fi
    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64)
            uv pip install -r "$req_file"
            ;;
        aarch64|arm64)
            local filtered
            filtered="$(mktemp)"
            # Drop torch / torchaudio / torchvision pins; Dramabox's 2.8 has
            # no aarch64+CUDA build.
            grep -v -i -E '^[[:space:]]*(torch|torchaudio|torchvision)([[:space:]]|=|<|>|~|!|$)' \
                "$req_file" > "$filtered"
            uv pip install -r "$filtered"
            rm -f "$filtered"
            uv pip install \
                --index-url https://download.pytorch.org/whl/cu130 \
                "torch>=2.10,<2.12" \
                "torchaudio>=2.10,<2.12"
            ;;
        *)
            echo "WARN: unknown arch '$arch'; installing Dramabox reqs verbatim." >&2
            uv pip install -r "$req_file"
            ;;
    esac
}

prepare_tts_env() {
    banner "Preparing venv for TTS phase (Dramabox runtime)"
    # uv sync first to keep tts/voice extras in place; then overlay
    # Dramabox's requirements via the platform-aware helper.
    uv sync --extra voice --extra tts
    install_dramabox_reqs

    # Sanity: refuse to proceed to Dramabox if torch.cuda isn't actually
    # available. Same check setup_dramabox.sh runs after a fresh install —
    # catches the case where a prior LLM-phase sync put a CPU-only wheel
    # back in place.
    if ! uv run python - <<'PY' >/dev/null 2>&1
import sys, torch
sys.exit(0 if torch.cuda.is_available() else 1)
PY
    then
        echo "" >&2
        echo "ERROR: torch.cuda is not available after TTS env preparation." >&2
        echo "  uname -m: $(uname -m)" >&2
        uv run python -c "import torch; print(f'  torch={torch.__version__}  cuda_available={torch.cuda.is_available()}')" >&2 || true
        echo "" >&2
        echo "Run ./scripts/setup_dramabox.sh manually and inspect its output." >&2
        exit 1
    fi
}

# ----- vLLM lifecycle --------------------------------------------------------

vllm_ready() {
    curl -sf "${LNVOX_VLLM_BASE%/v1}/v1/models" > /dev/null 2>&1
}

start_vllm() {
    if [ -n "$VLLM_URL" ]; then
        if ! vllm_ready; then
            echo "ERROR: --vllm-url=$VLLM_URL is not responding to /v1/models" >&2
            exit 1
        fi
        echo "Using external vLLM at $LNVOX_VLLM_BASE"
        return 0
    fi

    if vllm_ready; then
        echo "vLLM is already running at $LNVOX_VLLM_BASE — reusing it."
        return 0
    fi

    VLLM_LOG="$(mktemp -t lnvox_vllm.XXXXXX.log)"
    echo "Starting vLLM in background (log: $VLLM_LOG)…"
    nohup ./scripts/serve_vllm.sh > "$VLLM_LOG" 2>&1 &
    VLLM_PID=$!
    echo "vLLM PID: $VLLM_PID"

    local timeout=600
    local elapsed=0
    while ! vllm_ready; do
        if ! kill -0 "$VLLM_PID" 2>/dev/null; then
            echo ""
            echo "ERROR: vLLM died during startup. Last 30 lines of log:" >&2
            tail -30 "$VLLM_LOG" >&2 || true
            exit 1
        fi
        if [ "$elapsed" -ge "$timeout" ]; then
            echo "ERROR: vLLM did not become ready within ${timeout}s." >&2
            tail -30 "$VLLM_LOG" >&2 || true
            stop_vllm
            exit 1
        fi
        printf "."
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo ""
    echo "vLLM ready after ${elapsed}s."
}

stop_vllm() {
    if [ -z "${VLLM_PID:-}" ]; then
        return 0
    fi
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        VLLM_PID=""
        return 0
    fi
    echo "Stopping vLLM (PID $VLLM_PID)…"
    kill "$VLLM_PID" 2>/dev/null || true
    local elapsed=0
    while kill -0 "$VLLM_PID" 2>/dev/null; do
        if [ "$elapsed" -ge 30 ]; then
            echo "vLLM did not exit cleanly; sending SIGKILL"
            kill -9 "$VLLM_PID" 2>/dev/null || true
            break
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    VLLM_PID=""
    echo "vLLM stopped."
}

cleanup() {
    stop_vllm
}
trap cleanup EXIT INT TERM

# ----- LLM phase -------------------------------------------------------------

if [ "$SKIP_LLM" -eq 1 ]; then
    banner "Skipping LLM phase (--skip-llm)"
else
    banner "Starting LLM phase"
    if [ -z "$VLLM_URL" ]; then
        prepare_llm_env
    fi
    start_vllm

    banner "Stage 0: ingest"
    uv run lnvox ingest "$NOVEL_DIR" --book-id "$BOOK_ID"

    banner "Stage 1: cast extraction (with cross-volume merge if applicable)"
    uv run lnvox s1 "$BOOK_ID"

    banner "Stage 2: scene segmentation"
    uv run lnvox s2 "$BOOK_ID"

    banner "Stage V: voice cast"
    # Narrator handling:
    #   - --narrator-clip given           → use it (overrides any prior).
    #   - Not given AND prior volume      → matcher inherits prior Narrator clip.
    #   - Not given AND no prior volume   → matcher auto-casts the Narrator.
    if [ -n "$NARRATOR_CLIP" ]; then
        uv run lnvox voice cast "$BOOK_ID" --narrator-clip "$NARRATOR_CLIP"
    else
        uv run lnvox voice cast "$BOOK_ID"
    fi

    banner "Stage 3: director (uses voice cast metadata)"
    uv run lnvox s3 "$BOOK_ID" --regen-profiles

    banner "LLM phase complete — stopping vLLM to free GPU for Dramabox"
    stop_vllm
fi

# ----- TTS phase -------------------------------------------------------------

if [ "$SKIP_TTS" -eq 1 ]; then
    banner "Skipping TTS phase (--skip-tts)"
else
    # Even after we kill vLLM, give the GPU a moment to release VRAM before
    # Dramabox tries to claim it. Auto-managed lifecycle, but the kernel-level
    # release isn't instantaneous.
    if [ -z "$VLLM_URL" ]; then
        sleep 5
    fi
    prepare_tts_env
    banner "Stage 4: TTS (Dramabox, with auto-retry)"
    MAX_ATTEMPTS="$MAX_RETRIES" ./scripts/s4_retry.sh "$BOOK_ID"
fi

# ----- Mix phase -------------------------------------------------------------

if [ "$SKIP_MIX" -eq 1 ]; then
    banner "Skipping mix phase (--skip-mix)"
else
    banner "Stage 5: mix → m4b"
    uv run lnvox s5 "$BOOK_ID" --title "$BOOK_TITLE"
fi

banner "Pipeline complete."
echo "Final output should be at: $BOOK_ART/06_final/$BOOK_TITLE.m4b"
ls -la "$BOOK_ART/06_final/" 2>/dev/null || true
