#!/usr/bin/env bash
# Launch native llama-server (from llama.cpp) with OpenAI-compatible API on
# localhost:8000 — the third LLM backend, peer to serve_vllm.sh (Linux+CUDA)
# and serve_mlx.sh (Apple Silicon). See DESIGN.md §14.
#
# llama-server is NOT a Python package; install it out-of-band:
#   macOS:  brew install llama.cpp
#   Linux:  build from https://github.com/ggerganov/llama.cpp
#           (cmake -B build -DGGML_CUDA=on && cmake --build build --target llama-server)
#
# Defaults (mirror serve_vllm.sh's Gemma 4 conventions):
#   LNVOX_LLM_MODEL=google/gemma-4-E4B-it-qat-q4_0-gguf
#                            (Google's official QAT-quantized GGUF for the
#                            dev model; same E4B used by serve_vllm.sh.
#                            HF repo id is auto-downloaded; append :Q4_0 etc.
#                            to pick a quant inside multi-file repos. A
#                            local .gguf path also works.)
#                            Heavier prod pick:
#                            google/gemma-4-31B-it-qat-q4_0-gguf
#   LNVOX_LLM_PORT=8000
#   LNVOX_LLM_HOST=127.0.0.1
#   LNVOX_LLM_N_GPU_LAYERS=999       (full offload; 0 = CPU-only)
#   LNVOX_LLM_MAX_LEN=65536          (context window; matches serve_vllm.sh)
#
# Known limitations vs vLLM (accepted in §14.4):
#   - No `guided_json` enforcement (client retries handle parse failures).
#   - Prefix caching is bounded by `--cache-reuse` (256 by default).
#   - `repetition_penalty` IS honored (unlike mlx-lm).
set -euo pipefail

if ! command -v llama-server >/dev/null 2>&1; then
    echo "ERROR: 'llama-server' not on PATH." >&2
    echo "  Install via:" >&2
    echo "    macOS:  brew install llama.cpp" >&2
    echo "    Linux:  build from https://github.com/ggerganov/llama.cpp" >&2
    echo "  See DESIGN.md §14.2." >&2
    exit 127
fi

# Accept CLI flags as overrides for the LNVOX_LLM_* env vars. This mirrors the
# flag names run_pipeline.sh uses so the script is usable standalone (the env-
# var-only API was a UX trap: `./serve_llama.sh --llm-model X` silently used
# the default model).
while [ $# -gt 0 ]; do
    case "$1" in
        --llm-model)      export LNVOX_LLM_MODEL="$2";          shift 2 ;;
        --port)           export LNVOX_LLM_PORT="$2";           shift 2 ;;
        --host)           export LNVOX_LLM_HOST="$2";           shift 2 ;;
        --max-model-len)  export LNVOX_LLM_MAX_LEN="$2";        shift 2 ;;
        --n-gpu-layers)   export LNVOX_LLM_N_GPU_LAYERS="$2";   shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--llm-model ID] [--port N] [--host HOST] [--max-model-len N] [--n-gpu-layers N]"
            echo "  All flags also readable as LNVOX_LLM_{MODEL,PORT,HOST,MAX_LEN,N_GPU_LAYERS} env vars."
            exit 0
            ;;
        *) echo "ERROR: unknown arg '$1'. Run with --help." >&2; exit 2 ;;
    esac
done

MODEL="${LNVOX_LLM_MODEL:-google/gemma-4-E4B-it-qat-q4_0-gguf}"
PORT="${LNVOX_LLM_PORT:-8000}"
HOST="${LNVOX_LLM_HOST:-127.0.0.1}"
N_GPU_LAYERS="${LNVOX_LLM_N_GPU_LAYERS:-999}"
MAX_LEN="${LNVOX_LLM_MAX_LEN:-65536}"

# Model selection: a local file uses -m, anything else is treated as an HF
# repo id and uses -hf (which auto-downloads). The :quant suffix lets users
# pick a specific file inside a multi-quant repo, e.g.
#   LNVOX_LLM_MODEL=google/gemma-4-12B-it-qat-q4_0-gguf
MODEL_ARGS=()
if [ -f "$MODEL" ]; then
    MODEL_ARGS+=(-m "$MODEL")
else
    MODEL_ARGS+=(-hf "$MODEL")
fi

echo "Starting llama-server: model=$MODEL  host=$HOST  port=$PORT  n_gpu_layers=$N_GPU_LAYERS  ctx=$MAX_LEN"
# --jinja: use the chat template embedded in the GGUF. Required for newer
#   models whose template doesn't match llama.cpp's built-in fallbacks.
#   (DO NOT pair with --chat-template <name>; the built-in `gemma` template
#   is the Gemma 1/2 format and produces a malformed prompt for Gemma 4 —
#   the model falls back to raw text completion and hallucinates training-
#   data continuations like "gemma-7b-it: ## Prompt: A company is launching
#   a new smart thermostat …".)
# --reasoning-format auto: parse the GGUF template's reasoning channel (if
#   present) into choices[0].message.reasoning_content and keep the final
#   answer in choices[0].message.content. LLMClient.structured() reads
#   content first and falls back to reasoning_content. With `none` instead,
#   thinking-mode models like Gemma 4 emit <|channel>thought blocks that
#   land in content as prose, the JSON parser can't recover them, and the
#   model often burns the entire budget thinking before reaching <|channel>final.
exec llama-server \
    "${MODEL_ARGS[@]}" \
    --host "$HOST" \
    --port "$PORT" \
    --n-gpu-layers "$N_GPU_LAYERS" \
    --ctx-size "$MAX_LEN" \
    --cache-reuse 256 \
    --jinja \
    --reasoning-format auto
