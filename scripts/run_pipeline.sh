#!/usr/bin/env bash
# Run the full ln-vox pipeline for one volume of a series.
#
# Usage:
#     ./scripts/run_pipeline.sh <series>/<volume-XX> [options]
#
# Options:
#     --mode <m>               narration (default, multi-voice novel),
#                              lecture (single-voice non-fiction, §13), or
#                              scenario (theater script → sync + audio, §17).
#     --scenario-file <path>   Scenario mode: the script markdown to ingest
#                              (omit to reuse a prior ingest-scenario run).
#     --no-normalize           Lecture mode: skip the LLM speech-normalize pass
#                              (read text verbatim — no LLM phase at all).
#     --narrator-clip <id>     Voicebank clip id for the Narrator. Optional when
#                              a previous volume of the same series already has
#                              one assigned — it'll be inherited automatically.
#     --book-title <title>     Final m4b title (default: book_id).
#     --novels-root <path>     Where the chapter .txt files live (default: novels).
#     --vllm-url <url>         Use an already-running LLM server at this URL
#                              (skip auto start/stop). Flag name is historical;
#                              applies to whichever backend you've selected.
#     --llm-backend <name>     Which LLM server to launch: vllm | mlx | llama.
#                              Default: mlx on Darwin, vllm elsewhere. See
#                              DESIGN.md §14 for the llama-server (GGUF) path.
#                              Honors $LNVOX_LLM_BACKEND.
#     --llm-model <id>         Model id for the selected backend (HF repo,
#                              GGUF repo, or local path depending on backend).
#                              Default: google/gemma-4-E4B-it for vllm.
#     --max-model-len <N>      Override vLLM context window (default: model-dependent).
#     --skip-llm               Skip s1..s3 + voice cast (assume already done).
#     --skip-tts               Skip s4 (assume already rendered).
#     --staged-tts             Run s4 via the staged pipeline (DESIGN.md §15):
#                              checkpointed single-model phases with built-in
#                              crash resume. Honors $LNVOX_S4_STAGED=1.
#                              Dramabox-only.
#     --tts-backend <name>     TTS engine for s4: dramabox (default, per-beat)
#                              or vibevoice (multi-speaker sessions →
#                              05_audio_v2/; s5 mixes with --v2, s6 is
#                              skipped). Honors $LNVOX_TTS_BACKEND. See §16.
#     --skip-mix               Skip s5 (skip the m4b assembly).
#     --skip-sync              Skip s6 (skip the synced-EPUB / sync_manifest).
#     --epub <path>            Source EPUB for s6 (default: epubs/<book_id>.epub).
#     --max-retries N          Auto-retry budget for s4 (default 30).
#     --step-retries N         Auto-retry budget per non-TTS step (default 3).
#
# LLM lifecycle: by default the launcher picks an LLM backend, starts the
# matching serve script in the background, waits for it to be ready, runs
# the LLM-phase stages, then stops it (freeing GPU memory for Dramabox).
# Pass --vllm-url to skip this if you already have a server running.
#
# Backends (DESIGN.md §4 / §11 / §14):
#   - vllm  : scripts/serve_vllm.sh   (Linux + CUDA, default on Linux)
#   - mlx   : scripts/serve_mlx.sh    (Apple Silicon, default on Darwin)
#   - llama : scripts/serve_llama.sh  (native llama-server, GGUF, either OS)
# Pick one with --llm-backend or $LNVOX_LLM_BACKEND. The TTS phase uses
# Dramabox by default; pick VibeVoice sessions with --tts-backend vibevoice
# (DESIGN.md §16). --device mps is auto-selected on Darwin.

set -uo pipefail

OS="$(uname -s)"

BOOK_ID=""
MODE="narration"
SCENARIO_FILE=""
NO_NORMALIZE=0
NARRATOR_CLIP=""
BOOK_TITLE=""
NOVELS_ROOT="novels"
VLLM_URL=""
LLM_BACKEND="${LNVOX_LLM_BACKEND:-}"
LLM_MODEL=""
MAX_MODEL_LEN=""
SKIP_LLM=0
SKIP_TTS=0
STAGED_TTS="${LNVOX_S4_STAGED:-1}"
# Records whether staging was *asked for* (env var set or flag passed), as
# opposed to inherited from the default above — vibevoice ignores the
# default silently but rejects an explicit request (DESIGN.md §16.2).
STAGED_TTS_EXPLICIT="${LNVOX_S4_STAGED+1}"
TTS_BACKEND="${LNVOX_TTS_BACKEND:-dramabox}"
SKIP_MIX=0
SKIP_SYNC=0
EPUB_PATH=""
MAX_RETRIES="${MAX_RETRIES:-30}"
STEP_RETRIES="${STEP_RETRIES:-3}"
RETRY_DELAY="${RETRY_DELAY:-5}"

VLLM_PID=""
VLLM_LOG=""

usage() {
    sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage 0 ;;
        --mode)          MODE="$2";          shift 2 ;;
        --scenario-file) SCENARIO_FILE="$2"; shift 2 ;;
        --no-normalize)  NO_NORMALIZE=1;     shift   ;;
        --narrator-clip) NARRATOR_CLIP="$2"; shift 2 ;;
        --book-title)    BOOK_TITLE="$2";   shift 2 ;;
        --novels-root)   NOVELS_ROOT="$2";  shift 2 ;;
        --vllm-url)      VLLM_URL="$2";     shift 2 ;;
        --llm-backend)   LLM_BACKEND="$2";  shift 2 ;;
        --llm-model)     LLM_MODEL="$2";    shift 2 ;;
        --max-model-len) MAX_MODEL_LEN="$2"; shift 2 ;;
        --skip-llm)      SKIP_LLM=1;        shift   ;;
        --skip-tts)      SKIP_TTS=1;        shift   ;;
        --staged-tts)    STAGED_TTS=1; STAGED_TTS_EXPLICIT=1; shift ;;
        --tts-backend)   TTS_BACKEND="$2";  shift 2 ;;
        --skip-mix)      SKIP_MIX=1;        shift   ;;
        --skip-sync)     SKIP_SYNC=1;       shift   ;;
        --epub)          EPUB_PATH="$2";    shift 2 ;;
        --max-retries)   MAX_RETRIES="$2";  shift 2 ;;
        --step-retries)  STEP_RETRIES="$2"; shift 2 ;;
        -*)              echo "Unknown option: $1" >&2; usage 2 ;;
        *)               if [ -z "$BOOK_ID" ]; then BOOK_ID="$1"; shift; else echo "Extra positional: $1" >&2; usage 2; fi ;;
    esac
done

if [ -z "$BOOK_ID" ]; then
    echo "ERROR: book id (e.g. 'toaru/volume-01') is required" >&2
    usage 2
fi

# Resolve LLM backend: explicit --llm-backend / $LNVOX_LLM_BACKEND wins;
# otherwise default to mlx on Darwin and vllm everywhere else. See §14.
if [ -z "$LLM_BACKEND" ]; then
    if [ "$OS" = "Darwin" ]; then
        LLM_BACKEND="mlx"
    else
        LLM_BACKEND="vllm"
    fi
fi
case "$LLM_BACKEND" in
    vllm|mlx|llama) ;;
    *)
        echo "ERROR: --llm-backend must be one of vllm|mlx|llama (got '$LLM_BACKEND')" >&2
        usage 2
        ;;
esac

case "$TTS_BACKEND" in
    dramabox|vibevoice) ;;
    *)
        echo "ERROR: --tts-backend must be one of dramabox|vibevoice (got '$TTS_BACKEND')" >&2
        usage 2
        ;;
esac
if [ "$TTS_BACKEND" = "vibevoice" ] && [ -n "${STAGED_TTS_EXPLICIT:-}" ] && [ "$STAGED_TTS" = "1" ]; then
    echo "ERROR: --staged-tts is Dramabox-only (DESIGN.md §16.2); vibevoice uses the monolithic path + retry loop." >&2
    usage 2
fi

if [ "$MODE" != "narration" ] && [ "$MODE" != "lecture" ] && [ "$MODE" != "scenario" ]; then
    echo "ERROR: --mode must be 'narration', 'lecture' or 'scenario' (got '$MODE')" >&2
    usage 2
fi
if [ "$MODE" = "scenario" ] && [ -z "$SCENARIO_FILE" ] && [ ! -f "artifacts/$BOOK_ID/00_script.json" ]; then
    echo "ERROR: scenario mode needs --scenario-file <script.md> (or a prior ingest at artifacts/$BOOK_ID/00_script.json)" >&2
    usage 2
fi

# Does the LLM phase need a server at all? Narration always does. Lecture needs
# it for the speech-normalize pass and/or auto-casting the Narrator — but a
# lecture run with --no-normalize AND an explicit --narrator-clip makes zero LLM
# calls, so we can skip standing up vLLM entirely.
NEED_LLM=1
if [ "$MODE" = "lecture" ] && [ "$NO_NORMALIZE" -eq 1 ] && [ -n "$NARRATOR_CLIP" ]; then
    NEED_LLM=0
fi

BOOK_TITLE="${BOOK_TITLE:-$BOOK_ID}"
NOVEL_DIR="$NOVELS_ROOT/$BOOK_ID"
BOOK_ART="artifacts/$BOOK_ID"

if [ "$MODE" != "scenario" ] && [ ! -d "$NOVEL_DIR" ] && [ "$SKIP_LLM" -eq 0 ]; then
    echo "ERROR: novel dir not found: $NOVEL_DIR" >&2
    exit 1
fi

# Pin the lnvox CLI client to the right vLLM URL.
if [ -n "$VLLM_URL" ]; then
    export LNVOX_LLM__ENDPOINT="$VLLM_URL"
fi
LNVOX_VLLM_BASE="${LNVOX_LLM__ENDPOINT:-http://localhost:8000/v1}"

# Propagate model + max-model-len to whichever serve script we picked.
if [ -n "$LLM_MODEL" ]; then
    export LNVOX_LLM_MODEL="$LLM_MODEL"
    echo "Using LLM model: $LLM_MODEL"
fi
if [ -n "$MAX_MODEL_LEN" ]; then
    export LNVOX_LLM_MAX_LEN="$MAX_MODEL_LEN"
fi
echo "Using LLM backend: $LLM_BACKEND"

banner() {
    echo ""
    echo "############################################################"
    echo "## $1"
    echo "############################################################"
}

# Run a command, retrying up to STEP_RETRIES times on failure. The pipeline
# stages are idempotent (completed chapters/beats are cached), so a retry
# resumes from the failure instead of redoing finished work. Aborts the whole
# pipeline if the step still fails after the budget, or immediately on a
# signal (Ctrl-C / kill) so retries can't swallow an intentional stop.
#
#   run_step <hook|-> "<description>" <command> [args...]
#
# <hook> is a function name run BEFORE each retry (or "-" for none) — LLM steps
# pass `ensure_vllm` so a server that died mid-step is relaunched before the
# next attempt; the most common multi-retry failure is a crashed vLLM.
run_step() {
    local hook="$1"; shift
    local desc="$1"; shift
    local attempt=1
    while true; do
        "$@" && return 0
        local rc=$?
        if [ "$rc" -ge 128 ]; then
            echo "ERROR: '$desc' terminated by signal (exit $rc); aborting." >&2
            exit "$rc"
        fi
        if [ "$attempt" -ge "$STEP_RETRIES" ]; then
            echo "ERROR: '$desc' failed after $STEP_RETRIES attempt(s) (exit $rc); aborting." >&2
            exit "$rc"
        fi
        echo "WARN: '$desc' failed (exit $rc). Retry $attempt/$((STEP_RETRIES - 1)) in ${RETRY_DELAY}s…" >&2
        sleep "$RETRY_DELAY"
        if [ "$hook" != "-" ]; then
            "$hook"
        fi
        attempt=$((attempt + 1))
    done
}

# ----- Dependency phase management -------------------------------------------
#
# vLLM and Dramabox require incompatible torch / torchaudio versions:
#   vLLM>=0.19   → torchaudio>=2.10
#   Dramabox     → torch==2.8.0 / torchaudio==2.8.0 (per its requirements.txt)
# We swap the venv state between phases. uv is fast for no-op syncs, so the
# overhead is minimal when the state is already correct.

prepare_llm_env() {
    case "$LLM_BACKEND" in
        mlx)
            banner "Preparing venv for LLM phase (mlx-lm on Apple Silicon)"
            # mlx-lm path is much lighter than vLLM — mlx_lm pulls its own
            # MLX runtime and there's no torch ABI minefield to navigate.
            if [ -d .venv ] && ! uv run python -c "import mlx_lm" >/dev/null 2>&1; then
                echo "Detected broken venv (mlx_lm import fails). Recreating from scratch…"
                rm -rf .venv
            fi
            uv sync --extra mlx --extra voice --extra tts
            ;;

        llama)
            banner "Preparing venv for LLM phase (native llama-server)"
            # llama-server is an out-of-band native binary (DESIGN.md §14.2);
            # there's no Python LLM dep to install. We just want the voice +
            # tts extras synced for the TTS phase that follows.
            uv sync --extra voice --extra tts
            if ! command -v llama-server >/dev/null 2>&1; then
                echo "" >&2
                echo "ERROR: --llm-backend=llama but 'llama-server' is not on PATH." >&2
                echo "  Install via:  brew install llama.cpp   (macOS)" >&2
                echo "                or build from https://github.com/ggerganov/llama.cpp" >&2
                exit 1
            fi
            ;;

        vllm)
            banner "Preparing venv for LLM phase (vLLM-compatible torch)"
            # If torch can't even import (libcudnn / NCCL ABI mismatch after a
            # prior botched install), uv sync won't rescue us because the
            # lockfile thinks everything is already installed. Nuke the venv
            # and let uv rebuild.
            if [ -d .venv ] && ! uv run python -c "import torch, vllm" >/dev/null 2>&1; then
                echo "Detected broken venv (torch/vllm import fails). Recreating from scratch…"
                rm -rf .venv
            fi
            uv sync --extra serve --extra voice --extra tts
            ;;
    esac
}

# mamba-ssm / causal-conv1d ship only sdists on PyPI. Building those under
# uv's isolated build env links selective_scan_cuda against the build env's
# (latest) torch, not the venv's actual torch — the .so then fails to import
# ("undefined symbol: _ZN3c104cuda...") and RE-USE silently drops to its
# kernel-free fallback (~5-10x slower denoise). Install the prebuilt wheels
# from the upstream GitHub releases instead, keyed to the venv's exact
# torch/CUDA combo:
#   x86_64  → cu12torch2.8   (torch pinned 2.8.0+cu128 by Dramabox's reqs)
#   aarch64 → cu13torch2.10  (torch ==2.10.* from the cu130 index; 2.10 is
#             the newest torch the upstream wheel matrix covers, hence the
#             pin below instead of >=2.10,<2.12. Verified on DGX Spark GB10.)
MAMBA_SSM_VERSION="2.3.1"
CAUSAL_CONV1D_VERSION="1.6.2.post1"

install_mamba_kernel_wheels() {
    local abi="$1" arch="$2" pytag
    pytag="$(.venv/bin/python -c 'import sys; print(f"cp{sys.version_info[0]}{sys.version_info[1]}")')"
    uv pip install --no-deps \
        "https://github.com/Dao-AILab/causal-conv1d/releases/download/v${CAUSAL_CONV1D_VERSION}/causal_conv1d-${CAUSAL_CONV1D_VERSION}+${abi}cxx11abiTRUE-${pytag}-${pytag}-linux_${arch}.whl" \
        "https://github.com/state-spaces/mamba/releases/download/v${MAMBA_SSM_VERSION}/mamba_ssm-${MAMBA_SSM_VERSION}+${abi}cxx11abiTRUE-${pytag}-${pytag}-linux_${arch}.whl"
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

    # Darwin must be handled BEFORE the arm64 leg of the arch case — Apple
    # Silicon also reports arm64, but its needs are different (MPS torch +
    # no bitsandbytes). Mirrors setup_dramabox.sh's Darwin branch.
    if [ "$OS" = "Darwin" ]; then
        local filtered
        filtered="$(mktemp)"
        # Strip torch* AND bitsandbytes (no macOS wheel; DramaboxClient
        # disables bnb_4bit on MPS so it isn't needed).
        grep -v -i -E '^[[:space:]]*(torch|torchaudio|torchvision|bitsandbytes)([[:space:]]|=|<|>|~|!|$)' \
            "$req_file" > "$filtered"
        uv pip install -r "$filtered"
        rm -f "$filtered"
        uv pip install "torch>=2.10,<2.12" "torchaudio>=2.10,<2.12"
        return 0
    fi

    case "$arch" in
        x86_64)
            # Strip the mamba kernel packages so uv never builds their sdists
            # blind; install_mamba_kernel_wheels puts the ABI-matched prebuilt
            # wheels in their place.
            local filtered
            filtered="$(mktemp)"
            grep -v -i -E '^[[:space:]]*(mamba-ssm|causal-conv1d)([[:space:]]|=|<|>|~|!|$)' \
                "$req_file" > "$filtered"
            uv pip install -r "$filtered"
            rm -f "$filtered"
            install_mamba_kernel_wheels "cu12torch2.8" "x86_64"
            ;;
        aarch64|arm64)
            local filtered
            filtered="$(mktemp)"
            # Drop torch / torchaudio / torchvision pins (Dramabox's 2.8 has
            # no aarch64+CUDA build) AND the mamba kernel packages (sdist
            # builds link the wrong torch ABI — see install_mamba_kernel_wheels).
            grep -v -i -E '^[[:space:]]*(torch|torchaudio|torchvision|mamba-ssm|causal-conv1d)([[:space:]]|=|<|>|~|!|$)' \
                "$req_file" > "$filtered"
            uv pip install -r "$filtered"
            rm -f "$filtered"
            # ==2.10.* (not >=2.10,<2.12): the newest prebuilt mamba kernel
            # wheels are keyed cu13torch2.10 — torch 2.11 would leave RE-USE
            # on the slow kernel-free fallback. Bump both together when
            # upstream adds torch2.11 wheels.
            uv pip install \
                --index-url https://download.pytorch.org/whl/cu130 \
                "torch==2.10.*" \
                "torchaudio==2.10.*"
            install_mamba_kernel_wheels "cu13torch2.10" "aarch64"
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

    # A prior VibeVoice env prep pins transformers==4.51.3, which breaks
    # Dramabox's Gemma encoder (needs the ≥4.52 model structure — DESIGN.md
    # §16.6), and Dramabox's own '>=4.45' spec won't force the upgrade back.
    # Restore it explicitly; no-op when already satisfied.
    uv pip install "transformers>=4.52"

    # Sanity: refuse to proceed to Dramabox unless the expected torch
    # backend is actually available. Same check setup_dramabox.sh runs
    # after a fresh install — catches the case where a prior LLM-phase
    # sync put a CPU-only wheel back in place. Backend is MPS on Darwin,
    # CUDA elsewhere.
    local backend="cuda"
    if [ "$OS" = "Darwin" ]; then
        backend="mps"
    fi
    if ! uv run python - "$backend" <<'PY' >/dev/null 2>&1
import sys, torch
backend = sys.argv[1]
if backend == "mps":
    sys.exit(0 if torch.backends.mps.is_available() else 1)
sys.exit(0 if torch.cuda.is_available() else 1)
PY
    then
        echo "" >&2
        echo "ERROR: torch.$backend is not available after TTS env preparation." >&2
        echo "  uname -s: $OS  uname -m: $(uname -m)" >&2
        uv run python -c "import torch; print(f'  torch={torch.__version__}  cuda={torch.cuda.is_available()}  mps={torch.backends.mps.is_available()}')" >&2 || true
        echo "" >&2
        echo "Run ./scripts/setup_dramabox.sh manually and inspect its output." >&2
        exit 1
    fi
}

prepare_vibevoice_env() {
    banner "Preparing venv for TTS phase (VibeVoice runtime, DESIGN.md §16)"
    uv sync --extra voice --extra tts
    if [ ! -d "external/VibeVoice" ]; then
        echo "ERROR: external/VibeVoice not found — run ./scripts/setup_vibevoice.sh first." >&2
        exit 1
    fi
    uv pip install -e external/VibeVoice

    # Same torch-backend sanity check as prepare_tts_env: MPS on Darwin,
    # CUDA elsewhere — catches a CPU-only wheel left by an LLM-phase sync.
    local backend="cuda"
    if [ "$OS" = "Darwin" ]; then
        backend="mps"
    fi
    if ! uv run python - "$backend" <<'PY' >/dev/null 2>&1
import sys, torch
backend = sys.argv[1]
if backend == "mps":
    sys.exit(0 if torch.backends.mps.is_available() else 1)
sys.exit(0 if torch.cuda.is_available() else 1)
PY
    then
        echo "" >&2
        echo "ERROR: torch.$backend is not available after TTS env preparation." >&2
        echo "  uname -s: $OS  uname -m: $(uname -m)" >&2
        uv run python -c "import torch; print(f'  torch={torch.__version__}  cuda={torch.cuda.is_available()}  mps={torch.backends.mps.is_available()}')" >&2 || true
        echo "" >&2
        echo "Run ./scripts/setup_vibevoice.sh manually and inspect its output." >&2
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

    local serve_script backend_name
    case "$LLM_BACKEND" in
        vllm)  serve_script="./scripts/serve_vllm.sh";  backend_name="vLLM" ;;
        mlx)   serve_script="./scripts/serve_mlx.sh";   backend_name="mlx_lm.server" ;;
        llama) serve_script="./scripts/serve_llama.sh"; backend_name="llama-server" ;;
    esac
    VLLM_LOG="$(mktemp -t lnvox_llm.XXXXXX.log)"
    echo "Starting $backend_name in background (log: $VLLM_LOG)…"
    nohup "$serve_script" > "$VLLM_LOG" 2>&1 &
    VLLM_PID=$!
    echo "$backend_name PID: $VLLM_PID"

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

# Pre-retry hook for LLM steps: make sure a vLLM we manage is up before the
# next attempt. No-op for an external --vllm-url (we can't relaunch someone
# else's server) or when it's already healthy. `start_vllm` hard-exits if a
# fresh launch never becomes ready, which is the right call — a server that
# won't start is unrecoverable.
ensure_vllm() {
    if [ -n "$VLLM_URL" ]; then
        return 0
    fi
    if vllm_ready; then
        return 0
    fi
    echo "vLLM not responding before retry — relaunching…" >&2
    VLLM_PID=""  # old process is gone; let start_vllm launch a fresh one
    start_vllm
}

cleanup() {
    stop_vllm
}
trap cleanup EXIT INT TERM

# ----- LLM phase -------------------------------------------------------------

if [ "$SKIP_LLM" -eq 1 ]; then
    banner "Skipping LLM phase (--skip-llm)"
else
    banner "Starting LLM phase (mode=$MODE)"
    if [ "$NEED_LLM" -eq 1 ] && [ -z "$VLLM_URL" ]; then
        prepare_llm_env
    fi
    if [ "$NEED_LLM" -eq 1 ]; then
        start_vllm
    else
        banner "Lecture + --no-normalize + --narrator-clip: no LLM needed, skipping vLLM"
    fi

    # Pre-retry hooks relaunch a dead vLLM — only meaningful when we manage one.
    VC_HOOK="-"; LEC_HOOK="-"
    [ "$NEED_LLM" -eq 1 ] && { VC_HOOK="ensure_vllm"; LEC_HOOK="ensure_vllm"; }

    if [ "$MODE" = "scenario" ]; then
        # ingest-scenario is itself an LLM pass (structure + characters);
        # skipped when the artifacts already exist and no file was passed.
        if [ -n "$SCENARIO_FILE" ]; then
            banner "Stage 0: ingest-scenario (structure + characters)"
            run_step ensure_vllm "Stage 0 (ingest-scenario)" uv run lnvox ingest-scenario "$SCENARIO_FILE" --id "$BOOK_ID"
        else
            banner "Stage 0: ingest-scenario skipped (artifacts/$BOOK_ID/00_script.json exists)"
        fi
    else
        banner "Stage 0: ingest (mode=$MODE)"
        run_step - "Stage 0 (ingest)" uv run lnvox ingest "$NOVEL_DIR" --book-id "$BOOK_ID" --mode "$MODE"
    fi

    if [ "$MODE" = "narration" ]; then
        banner "Stage 1: cast extraction (with cross-volume merge if applicable)"
        run_step ensure_vllm "Stage 1 (cast extraction)" uv run lnvox s1 "$BOOK_ID"

        banner "Stage 2: scene segmentation"
        run_step ensure_vllm "Stage 2 (scene segmentation)" uv run lnvox s2 "$BOOK_ID"
    else
        banner "Skipping s1 (characters) and s2 (scenes) — mode=$MODE"
    fi

    banner "Stage V: voice cast"
    # Narrator handling:
    #   - --narrator-clip given           → use it (overrides any prior).
    #   - Not given AND prior volume      → matcher inherits prior Narrator clip.
    #   - Not given AND no prior volume   → matcher auto-casts the Narrator.
    if [ -n "$NARRATOR_CLIP" ]; then
        run_step "$VC_HOOK" "Stage V (voice cast)" uv run lnvox voice cast "$BOOK_ID" --narrator-clip "$NARRATOR_CLIP"
    else
        run_step "$VC_HOOK" "Stage V (voice cast)" uv run lnvox voice cast "$BOOK_ID"
    fi

    if [ "$MODE" = "narration" ]; then
        banner "Stage 3: director (uses voice cast metadata)"
        run_step ensure_vllm "Stage 3 (director)" uv run lnvox s3 "$BOOK_ID" --regen-profiles
    elif [ "$MODE" = "scenario" ]; then
        banner "Stage S: scenario direction (emotion + cues → directed beats)"
        run_step ensure_vllm "Stage S (scenario)" uv run lnvox scenario "$BOOK_ID"
    else
        banner "Stage L: lecture (split + speech-normalize → directed beats)"
        if [ "$NO_NORMALIZE" -eq 1 ]; then
            run_step "$LEC_HOOK" "Stage L (lecture)" uv run lnvox lecture "$BOOK_ID" --no-normalize
        else
            run_step "$LEC_HOOK" "Stage L (lecture)" uv run lnvox lecture "$BOOK_ID"
        fi
    fi

    if [ "$NEED_LLM" -eq 1 ]; then
        banner "LLM phase complete — stopping vLLM to free GPU for Dramabox"
        stop_vllm
    fi
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
    if [ "$TTS_BACKEND" = "vibevoice" ]; then
        prepare_vibevoice_env
        # Single-model boot — no staged phases (DESIGN.md §16.2); the retry
        # loop's restart cost is just that boot. The env var routes the
        # `lnvox s4` inside s4_retry.sh to the vibevoice backend.
        banner "Stage 4: TTS (VibeVoice sessions → 05_audio_v2, with auto-retry)"
        MAX_ATTEMPTS="$MAX_RETRIES" LNVOX_TTS_BACKEND=vibevoice ./scripts/s4_retry.sh "$BOOK_ID"
    elif [ "$STAGED_TTS" -eq 1 ]; then
        prepare_tts_env
        # The staged driver owns crash retries internally (per-phase
        # subprocesses with file-level resume, DESIGN.md §15.4) — no outer
        # retry loop, so a driver abort (stall limit) surfaces immediately.
        banner "Stage 4: TTS (Dramabox, staged phases)"
        if ! uv run lnvox s4 "$BOOK_ID" --staged; then
            echo "ERROR: staged s4 failed — see the stall diagnostics above." >&2
            exit 1
        fi
    else
        prepare_tts_env
        banner "Stage 4: TTS (Dramabox, with auto-retry)"
        MAX_ATTEMPTS="$MAX_RETRIES" ./scripts/s4_retry.sh "$BOOK_ID"
    fi
fi

# ----- Mix phase -------------------------------------------------------------

if [ "$SKIP_MIX" -eq 1 ]; then
    banner "Skipping mix phase (--skip-mix)"
else
    banner "Stage 5: mix → m4b"
    if [ "$TTS_BACKEND" = "vibevoice" ]; then
        run_step - "Stage 5 (mix)" uv run lnvox s5 "$BOOK_ID" --title "$BOOK_TITLE" --v2
    else
        run_step - "Stage 5 (mix)" uv run lnvox s5 "$BOOK_ID" --title "$BOOK_TITLE"
    fi
fi

# ----- Sync phase (Stage 6, CPU-only) ----------------------------------------
#
# Re-aligns the directed beats onto the original EPUB → a synced EPUB +
# sync_manifest.json the ln-reader uses to highlight text in time with the
# audio (and, in lecture mode, to flip to code/table/figure visual elements).
# Needs the source EPUB and the rendered audio, so it's skipped gracefully for
# a .txt-only book or when TTS hasn't run. Stage-6 silence defaults match
# Stage 5's defaults used above.
if [ "$SKIP_SYNC" -eq 1 ]; then
    banner "Skipping sync phase (--skip-sync)"
elif [ "$MODE" = "scenario" ]; then
    if [ "$TTS_BACKEND" = "vibevoice" ]; then
        banner "Skipping scenario-sync (VibeVoice sessions have no per-line timing — DESIGN.md §17.4)"
    else
        banner "Scenario sync: timed sync files (DESIGN.md §17.4)"
        run_step - "Scenario sync" uv run lnvox scenario-sync "$BOOK_ID"
    fi
elif [ "$TTS_BACKEND" = "vibevoice" ]; then
    # A session WAV has no per-beat timestamps for the reader to anchor to;
    # recovering them needs forced alignment — future work (DESIGN.md §16.10).
    banner "Skipping sync phase (VibeVoice v2 audio has no per-beat timing)"
else
    SYNC_EPUB="${EPUB_PATH:-epubs/$BOOK_ID.epub}"
    if [ ! -f "$SYNC_EPUB" ]; then
        banner "Stage 6: skipped (no source EPUB at $SYNC_EPUB)"
        echo "Place the EPUB at epubs/$BOOK_ID.epub or pass --epub <path> to enable sync."
    elif [ ! -d "$BOOK_ART/05_audio" ]; then
        banner "Stage 6: skipped (no rendered audio at $BOOK_ART/05_audio)"
    else
        banner "Stage 6: sync layer (EPUB beat-spans + sync_manifest.json)"
        run_step - "Stage 6 (sync)" uv run lnvox s6 "$BOOK_ID" \
            --epub "$SYNC_EPUB" --novels-root "$NOVELS_ROOT"
    fi
fi

banner "Pipeline complete."
echo "Final output should be at: $BOOK_ART/06_final/$BOOK_TITLE.m4b"
ls -la "$BOOK_ART/06_final/" 2>/dev/null || true
if [ -f "$BOOK_ART/07_sync/sync_manifest.json" ]; then
    echo "Sync layer:               $BOOK_ART/07_sync/ (synced EPUB + sync_manifest.json)"
fi
