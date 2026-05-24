#!/usr/bin/env bash
# Launch vLLM serving Gemma 4 with OpenAI-compatible API on localhost:8000.
#
# Defaults:
#   LNVOX_LLM_MODEL=google/gemma-4-E4B-it      (dev, fast, ~10GB VRAM)
#   LNVOX_LLM_MAX_LEN=65536                    (context window)
#
# Recommended for quality:
#   LNVOX_LLM_MODEL=nvidia/Gemma-4-31B-IT-NVFP4 (NVFP4-quantised 31B,
#                                                ~16GB weights + ~7GB KV-cache
#                                                at max-model-len=32768)
#   LNVOX_LLM_MAX_LEN=32768  (lower context to fit on 32GB cards)
#
# NVFP4 needs an NVIDIA Blackwell GPU (RTX 50-series, H100/H200, B-series).
set -euo pipefail

MODEL="${LNVOX_LLM_MODEL:-google/gemma-4-E4B-it}"
PORT="${LNVOX_LLM_PORT:-8000}"
# Bigger models need a larger memory budget but can't claim more than the
# desktop leaves free. On a 32GB card with a typical Linux desktop
# (~2.5-3GB used by Chrome/Wayland/etc), 0.88 is the safe ceiling.
if [[ "$MODEL" == *31B* || "$MODEL" == *27B* ]]; then
    GPU_UTIL="${LNVOX_LLM_GPU_UTIL:-0.88}"
    export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
else
    GPU_UTIL="${LNVOX_LLM_GPU_UTIL:-0.85}"
fi

# Pick a sensible default max-model-len based on the model. NVFP4 31B is
# memory-tight; smaller defaults prevent CUDA OOM. Override via the env var.
if [[ "$MODEL" == *31B* || "$MODEL" == *27B* ]]; then
    # KV-cache grows linearly with context. At 31B with fp8 KV cache on a
    # 32GB card, 20K tokens leaves ~2GB of safety margin after weights +
    # activations + cudagraph buffers.
    DEFAULT_MAX_LEN=20480
else
    DEFAULT_MAX_LEN=65536
fi
MAX_LEN="${LNVOX_LLM_MAX_LEN:-$DEFAULT_MAX_LEN}"

# Per-model knobs.
EXTRA_ARGS=()
if [[ "$MODEL" == *NVFP4* || "$MODEL" == *nvfp4* ]]; then
    EXTRA_ARGS+=(--quantization nvfp4)
fi
# Gemma 4 / 3 are multimodal (image-aware). Their `max_tokens_per_mm_item` is
# 2496, larger than vLLM's default `max_num_batched_tokens=2048`, so without
# this override startup aborts with a Chunked-MM error.
if [[ "$MODEL" == *gemma*4* || "$MODEL" == *Gemma-4* || "$MODEL" == *gemma-3* || "$MODEL" == *Gemma-3* ]]; then
    EXTRA_ARGS+=(--max-num-batched-tokens 8192)
fi

echo "Starting vLLM: model=$MODEL  port=$PORT  max-model-len=$MAX_LEN"
if [ ${#EXTRA_ARGS[@]} -gt 0 ]; then
    echo "                extra args: ${EXTRA_ARGS[*]}"
fi
exec uv run --extra serve python -m vllm.entrypoints.openai.api_server \
    --model "$MODEL" \
    --port "$PORT" \
    --enable-prefix-caching \
    --max-model-len "$MAX_LEN" \
    --gpu-memory-utilization "$GPU_UTIL" \
    "${EXTRA_ARGS[@]}"
