#!/usr/bin/env bash
# Launch mlx_lm.server on Apple Silicon (M-series Mac) with an OpenAI-compatible
# API on localhost:8000 — the same contract serve_vllm.sh provides on Linux,
# so LLMClient runs unchanged. See DESIGN.md §11.2.
#
# Defaults:
#   LNVOX_LLM_MODEL=mlx-community/gemma-3-4b-it-4bit    (dev, ~4GB unified)
#
# Heavier picks (need 32GB+ unified memory):
#   LNVOX_LLM_MODEL=mlx-community/gemma-3-12b-it-4bit
#   LNVOX_LLM_MODEL=mlx-community/gemma-3-27b-it-4bit
#
# Known limitations vs vLLM (accepted in §11.2):
#   - No `guided_json` enforcement (client retries handle parse failures).
#   - No `repetition_penalty` knob.
#   - No prefix caching across calls.
set -euo pipefail

MODEL="${LNVOX_LLM_MODEL:-mlx-community/gemma-3-4b-it-4bit}"
PORT="${LNVOX_LLM_PORT:-8000}"
HOST="${LNVOX_LLM_HOST:-127.0.0.1}"

# mlx_lm.server has no `--max-model-len` knob — the context window comes
# from the model's own config. We still respect LNVOX_LLM_MAX_LEN by
# exporting it for the lnvox client (which uses it to clamp output budgets
# in LLMClient.budget_for).
if [ -n "${LNVOX_LLM_MAX_LEN:-}" ]; then
    export LNVOX_LLM_MAX_MODEL_LEN="$LNVOX_LLM_MAX_LEN"
fi

echo "Starting mlx_lm.server: model=$MODEL  host=$HOST  port=$PORT"
exec uv run --extra mlx python -m mlx_lm.server \
    --model "$MODEL" \
    --host "$HOST" \
    --port "$PORT"
