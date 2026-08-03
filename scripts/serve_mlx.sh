#!/usr/bin/env bash
# Launch mlx_lm.server on Apple Silicon (M-series Mac) with an OpenAI-compatible
# API on localhost:8000 — the same contract serve_vllm.sh provides on Linux,
# so LLMClient runs unchanged. See DESIGN.md §11.2.
#
# Default: mlx-community/gemma-4-12B-it-qat-4bit (~7 GB) — the MLX analog
# of the pipeline-wide llama default (google/gemma-4-12B-it-qat-q4_0-gguf,
# DESIGN.md §14.3). Its model_type `gemma4_unified` needs the git-pinned
# mlx-lm from pyproject's [tool.uv.sources] (fix merged upstream in
# ml-explore/mlx-lm#1349 but unreleased; PyPI ≤0.31.3 raises "Model type
# gemma4_unified not supported" and then HANGS the request — see
# MAC_TEST_REPORT.md F7).
#
# Alternates (older `gemma4`-type conversions; load on any mlx-lm):
#   LNVOX_LLM_MODEL=mlx-community/gemma-4-31B-it-qat-4bit  (32GB+ Macs;
#       ~3x slower than 12B — s2 took 33 min on ONE chapter on an M4 Max)
#   LNVOX_LLM_MODEL=mlx-community/gemma-4-E4B-it-4bit      (16GB Macs; weak —
#       expect s2-killing quality issues, MAC_TEST_REPORT.md F3/F5/F6)
#
# Known limitations vs vLLM (accepted in §11.2):
#   - No `guided_json` enforcement (client retries handle parse failures).
#   - No `repetition_penalty` knob.
#   - No prefix caching across calls.
set -euo pipefail

MODEL="${LNVOX_LLM_MODEL:-mlx-community/gemma-4-12B-it-qat-4bit}"
PORT="${LNVOX_LLM_PORT:-8000}"
HOST="${LNVOX_LLM_HOST:-127.0.0.1}"
# mlx-lm 0.31.3's batched decode path IGNORES per-request sampling params
# (verified: two identical requests at temperature=1.5 return byte-identical
# output) and falls back to the server-wide default, which is 0.0 — greedy.
# Greedy + no guided_json means a schema-invalid completion reproduces
# byte-identically on every retry, so LLMClient's validate-and-retry loop can
# never converge. Serve with a real sampling temperature (matches LLMConfig's
# 0.2 default) so retries at least draw fresh samples.
TEMP="${LNVOX_LLM_TEMP:-0.2}"

# mlx_lm.server has no `--max-model-len` knob — the context window comes
# from the model's own config. We still respect LNVOX_LLM_MAX_LEN by
# exporting it for the lnvox client (which uses it to clamp output budgets
# in LLMClient.budget_for).
if [ -n "${LNVOX_LLM_MAX_LEN:-}" ]; then
    export LNVOX_LLM_MAX_MODEL_LEN="$LNVOX_LLM_MAX_LEN"
fi

echo "Starting mlx_lm.server: model=$MODEL  host=$HOST  port=$PORT  temp=$TEMP"
# `python -m mlx_lm.server` is deprecated since mlx-lm's CLI consolidation
# (warns on ≥0.31); the supported form is the `mlx_lm server` subcommand.
exec uv run --extra mlx python -m mlx_lm server \
    --model "$MODEL" \
    --host "$HOST" \
    --port "$PORT" \
    --temp "$TEMP"
