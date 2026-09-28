---
title: Helper scripts
description: The LLM serve scripts, the Dramabox / VibeVoice installers and the s4 retry loop in scripts/ — flags, env vars, defaults, and how the launcher calls them.
sidebar:
  order: 3
  label: Helper scripts
---

Everything in `scripts/` besides the launcher itself (see [run_pipeline.sh](/launcher/run-pipeline/)) and the [Voicebank Studio](/guides/voicebank-studio/). The launcher calls the serve scripts and the retry loop for you. Run them by hand to bring up an LLM server for ad-hoc stage runs, to do the one-time TTS installs, or to render s4 outside the launcher.

| Script | What it does | Launcher use |
|---|---|---|
| `serve_llama.sh` | Starts native `llama-server` (llama.cpp, GGUF) | `start_vllm` when `--llm-backend llama` (default) |
| `serve_mlx.sh` | Starts `mlx_lm server` (Apple Silicon) | `start_vllm` when `--llm-backend mlx` |
| `serve_vllm.sh` | Starts the vLLM OpenAI server (Linux + CUDA) | `start_vllm` when `--llm-backend vllm` |
| `setup_dramabox.sh` | Clones Dramabox into `external/DramaBox`, installs its requirements for the platform | Not called. `prepare_tts_env` repeats the install step on every TTS phase |
| `setup_vibevoice.sh` | Clones the VibeVoice community fork into `external/VibeVoice`, installs it editable, can fetch weights | Not called. `prepare_vibevoice_env` requires it has run |
| `s4_retry.sh` | Loops `lnvox s4 <book>` until it exits 0 (stops at once on exit 2) | Monolithic Dramabox s4 (`LNVOX_S4_STAGED=0`) and `--tts-backend vibevoice` |

## LLM serve scripts

All three serve an OpenAI-compatible API at `http://127.0.0.1:8000/v1` by default. That is the endpoint [`LLMClient`](/library/llm/client/) expects, so every LLM stage runs against any of them unchanged. Backend trade-offs are in DESIGN.md §4, §11.2 and §14.

### How the launcher starts them

```bash title="scripts/run_pipeline.sh"
    local serve_script backend_name
    case "$LLM_BACKEND" in
        vllm)  serve_script="./scripts/serve_vllm.sh";  backend_name="vLLM" ;;
        mlx)   serve_script="./scripts/serve_mlx.sh";   backend_name="mlx_lm server" ;;
        llama) serve_script="./scripts/serve_llama.sh"; backend_name="llama-server" ;;
    esac
    VLLM_LOG="$(mktemp -t lnvox_llm.XXXXXX.log)"
    echo "Starting $backend_name in background (log: $VLLM_LOG)…"
    nohup "$serve_script" > "$VLLM_LOG" 2>&1 &
    VLLM_PID=$!
```

What `start_vllm` does around that block (it keeps the vLLM name for historical reasons, but serves every backend):

- **No flags, only env vars.** The launcher always exports `LNVOX_LLM_MODEL` (and the nested `LNVOX_LLM__MODEL` for the client): your `--llm-model`, else an exported `LNVOX_LLM__MODEL` / `LNVOX_LLM_MODEL`, else the backend's default (`google/gemma-4-12B-it-qat-q4_0-gguf` for llama, `google/gemma-4-E4B-it` for vllm, `mlx-community/gemma-4-12B-it-qat-4bit` for mlx). `--max-model-len N` exports `LNVOX_LLM_MAX_LEN` for the serve script and `LNVOX_LLM_MAX_MODEL_LEN` / `LNVOX_LLM__MAX_MODEL_LEN` for the client. Without it, starting `serve_vllm.sh` for a `31B` / `27B` model sets all three to 20480, the window that script picks for those models (unless a context length is already set somewhere).
- **Readiness.** Every 3 s it runs `curl -sf "${LNVOX_VLLM_BASE%/v1}/v1/models"`, where `LNVOX_VLLM_BASE` is `--vllm-url`, else `$LNVOX_LLM__ENDPOINT`, else `$LNVOX_LLM_ENDPOINT`, else `http://localhost:${LNVOX_LLM_PORT:-8000}/v1`. The same URL is exported to the stages as `LNVOX_LLM__ENDPOINT`, so the probe and the client always agree, and a custom `LNVOX_LLM_PORT` moves the server, the probe and the client together. If the server process exits it prints the last 30 log lines and aborts. After 600 s it stops the server and aborts.
- **Reuse.** If something already answers `/v1/models` at that URL, the launcher uses it and does not start a server. It also won't stop that server later, because it never had its PID. Whatever model that server loaded is the model you get.
- **Shutdown.** `stop_vllm` sends SIGTERM, waits up to 30 s, then SIGKILL. It runs after the LLM phase, on `--stop-after-audit`, and from the `EXIT`/`INT`/`TERM` trap.
- **Retries.** LLM steps run as `run_step ensure_vllm …`. Before each retry, `ensure_vllm` relaunches a server the launcher owns if it stopped answering. With `--vllm-url` it does nothing.

:::note[Running a serve script by hand]
Outside the launcher, the client derives nothing from the serve script. If you bind a serve script to another port, point the stages at it (`LNVOX_LLM_ENDPOINT=http://localhost:<port>/v1`). A context window set with `LNVOX_LLM_MAX_LEN` reaches the client too, because [config](/library/config/) reads it as a fallback for `LNVOX_LLM_MAX_MODEL_LEN`. But `serve_vllm.sh`'s built-in 20480 default for 31B/27B models doesn't, so export `LNVOX_LLM_MAX_LEN=20480` with it.
:::

### `serve_llama.sh`

Starts `llama-server` (llama.cpp). This is the default backend on every platform. `llama-server` is a native binary, not a Python package. Install it first (`brew install llama.cpp` on macOS, or a CUDA build from source on Linux). If it isn't on `PATH`, the script exits with code 127.

This is the only serve script that also takes CLI flags. Each flag sets the matching env var:

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--llm-model ID` | `LNVOX_LLM_MODEL` | `google/gemma-4-12B-it-qat-q4_0-gguf` | An existing local file loads with `-m`. Anything else is treated as a Hugging Face repo and loads with `-hf` (auto-download; append `:Q4_0` etc. to choose a quant) |
| `--port N` | `LNVOX_LLM_PORT` | `8000` | Listen port |
| `--host HOST` | `LNVOX_LLM_HOST` | `127.0.0.1` | Bind address |
| `--max-model-len N` | `LNVOX_LLM_MAX_LEN` | `65536` | Passed as `--ctx-size` |
| `--n-gpu-layers N` | `LNVOX_LLM_N_GPU_LAYERS` | `999` | Layers to offload (`0` = CPU only) |
| `-h`, `--help` | | | Prints usage. Any unknown argument exits with code 2 |

Fixed server flags: `--cache-reuse 256 --jinja --reasoning-format auto`. `--jinja` uses the chat template embedded in the GGUF. Don't add `--chat-template gemma`: that built-in template is the Gemma 1/2 format and gives Gemma 4 a malformed prompt. `--reasoning-format auto` moves thinking-channel output into `reasoning_content` and leaves the answer in `content`. [`LLMClient.structured`](/library/llm/client/) reads `content` first and falls back to `reasoning_content`.

```bash
./scripts/serve_llama.sh --llm-model google/gemma-4-31B-it-qat-q4_0-gguf --max-model-len 32768
```

### `serve_mlx.sh`

Starts `mlx_lm server` on Apple Silicon: `uv run --extra mlx python -m mlx_lm server --model … --host … --port … --temp …`. It reads **env vars only** and ignores any arguments.

| Env var | Default | Meaning |
|---|---|---|
| `LNVOX_LLM_MODEL` | `mlx-community/gemma-4-12B-it-qat-4bit` | MLX checkpoint (~7 GB). Needs the git-pinned `mlx-lm` from `pyproject.toml` |
| `LNVOX_LLM_PORT` | `8000` | Listen port |
| `LNVOX_LLM_HOST` | `127.0.0.1` | Bind address |
| `LNVOX_LLM_TEMP` | `0.2` | Server-wide sampling temperature (`--temp`) |
| `LNVOX_LLM_MAX_LEN` | ignored | `mlx_lm server` has no context-window flag; the window comes from the model's config. The launcher's `--max-model-len` still tells the client |

`--temp` is required, not tuning. mlx-lm 0.31.3 ignores per-request sampling parameters and falls back to the server-wide default of 0.0 (greedy). Under greedy decoding, a schema-invalid completion comes back identical on every retry, and the client's validate-and-retry loop never recovers (MAC_TEST_REPORT.md F5). Alternate models (they use the older `gemma4` model type, which any mlx-lm can load) are listed in the script header:

- `mlx-community/gemma-4-31B-it-qat-4bit`: needs a 32 GB+ Mac and is about 3× slower (s2 took 33 min on one chapter on an M4 Max).
- `mlx-community/gemma-4-E4B-it-4bit`: fits 16 GB Macs but is structurally unreliable on s1/s2.

The [Apple Silicon guide](/guides/apple-silicon/) covers the rest.

### `serve_vllm.sh`

Starts vLLM on Linux + CUDA: `uv run --extra serve python -m vllm.entrypoints.openai.api_server --model … --port … --enable-prefix-caching --max-model-len … --gpu-memory-utilization …`. It reads **env vars only**. No `--host` is passed, so vLLM uses its own default bind address.

| Env var | Default | Meaning |
|---|---|---|
| `LNVOX_LLM_MODEL` | `google/gemma-4-E4B-it` | HF model id. vLLM checks the request's model name strictly, which is why the launcher always exports this |
| `LNVOX_LLM_PORT` | `8000` | Listen port |
| `LNVOX_LLM_MAX_LEN` | `20480` if the model name contains `31B` or `27B`, else `65536` | `--max-model-len` |
| `LNVOX_LLM_GPU_UTIL` | `0.88` for `*31B*` / `*27B*`, else `0.85` | `--gpu-memory-utilization` |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` (31B/27B only, if unset) | Allocator config for the big models |

Extra arguments are added from the model name:

- `--quantization nvfp4` when the name contains `NVFP4` or `nvfp4`. NVFP4 needs a Blackwell GPU.
- `--max-num-batched-tokens 8192` for Gemma 3/4 names. Their multimodal `max_tokens_per_mm_item` is 2496, over vLLM's default of 2048, and startup aborts without this.

```bash
LNVOX_LLM_MODEL=nvidia/Gemma-4-31B-IT-NVFP4 LNVOX_LLM_MAX_LEN=32768 ./scripts/serve_vllm.sh
```

vLLM (the `serve` extra) and Dramabox need incompatible torch builds, so the launcher's `prepare_llm_env` / `prepare_tts_env` swap the venv between phases (see [launcher functions](/launcher/functions/)).

## TTS installers

Neither engine can be installed with plain `pip`. Both are cloned into `external/` and installed into the project's uv venv with `uv pip`. Packages installed this way are **not tracked by the lockfile**, and any later `uv sync` removes them. The launcher re-installs Dramabox's requirements (`install_dramabox_reqs`) and VibeVoice (`uv pip install -e external/VibeVoice`) at the start of every TTS phase for this reason.

### `setup_dramabox.sh`

1. Clones `https://github.com/resemble-ai/DramaBox.git` (`--depth 1`) into `external/DramaBox`, unless it already exists. To update, run `git -C external/DramaBox pull`.
2. Uses the first of `requirements.txt`, `requirements/requirements.txt` or `requirements_inference.txt` that exists. If none does, it lists the repo and exits 1.
3. Installs by platform. Darwin is checked **before** the arch, because Apple Silicon also reports `arm64`:

| Platform | Filtered out of Dramabox's requirements | Then installs |
|---|---|---|
| Darwin (Apple Silicon) | `torch`, `torchaudio`, `torchvision`, `bitsandbytes` (CUDA-only, no macOS wheel) | `torch>=2.10,<2.12` and `torchaudio>=2.10,<2.12` from PyPI (MPS-capable) |
| Linux `x86_64` | `mamba-ssm`, `causal-conv1d` (Dramabox's `torch==2.8.0+cu128` pin is kept) | Prebuilt wheels `causal_conv1d 1.6.2.post1` + `mamba_ssm 2.3.1`, ABI `cu12torch2.8cxx11abiTRUE`, `linux_x86_64`, from the upstream GitHub releases |
| Linux `aarch64` / `arm64` (DGX Spark, Grace Hopper, Jetson) | `torch`, `torchaudio`, `torchvision`, `mamba-ssm`, `causal-conv1d` | `torch==2.10.*` and `torchaudio==2.10.*` from `https://download.pytorch.org/whl/cu130`, then the same kernel wheels with ABI `cu13torch2.10cxx11abiTRUE`, `linux_aarch64` |
| anything else | nothing | the requirements file verbatim |

The mamba kernels come from prebuilt wheels because PyPI only ships sdists. uv builds those in an isolated env against the wrong torch ABI. The resulting `.so` fails to import, and RE-USE silently drops to its kernel-free fallback, which denoises about 5–10× slower. aarch64 is pinned to `==2.10.*` because the newest prebuilt kernel wheels are keyed to torch 2.10.

4. **Sanity check.** On Darwin it runs `torch.backends.mps.is_available()`. Elsewhere it runs `torch.cuda.is_available()` and prints the device name. On failure it exits 1 with a hint (the aarch64 hint is a `--force-reinstall` from the cu130 index). The first real render then downloads about 15 GB of weights from Hugging Face.

The x86/aarch64 branches read the Python tag from `.venv/bin/python`, so create the venv first (`uv sync --extra voice --extra tts`).

:::note[What the launcher does differently]
`install_dramabox_reqs` in `run_pipeline.sh` repeats these branches but only reads `external/DramaBox/requirements.txt`. It also tries the kernel wheels from the uv cache first (`--offline`). `prepare_tts_env` then adds `uv pip install "transformers>=4.52"` (a VibeVoice prep pins 4.51.3) and a stricter check that also imports `causal_conv1d_cuda` and `selective_scan_cuda`. After a manual `uv sync`, re-run `setup_dramabox.sh` before running s4 by hand.
:::

### `setup_vibevoice.sh`

```bash
./scripts/setup_vibevoice.sh                # clone + install only
./scripts/setup_vibevoice.sh --weights      # + VibeVoice-Large weights from ModelScope
./scripts/setup_vibevoice.sh --weights-hf   # + weights from Hugging Face
```

1. Clones `https://github.com/vibevoice-community/VibeVoice.git` into `external/VibeVoice` (a full clone, since a commit is checked out), then `git fetch` and `git checkout` the pinned ref. `LNVOX_VIBEVOICE_REF` overrides it (default `07cb79feadd2d3fd7f47530d4c964a12857936a0`).
2. `uv pip install -e external/VibeVoice`, then checks that `import vibevoice` works. There is no per-platform branch. VibeVoice uses whichever torch the Dramabox installer left in the venv.
3. Weights (about 18 GB) go to `models/VibeVoice-Large/`, and the download is skipped if `config.json` is already there. `--weights` runs `uvx modelscope download --model microsoft/VibeVoice-Large`. `--weights-hf` runs `uv run hf download`, falling back to `huggingface-cli download`. Any other argument exits with code 2.

At run time the client resolves the model as `$LNVOX_VIBEVOICE_MODEL`, then `models/VibeVoice-Large/` (if it has `config.json`), then the HF id `microsoft/VibeVoice-Large`, which downloads to the HF cache. See [VibeVoiceClient](/library/tts/vibevoice-client/).

:::caution
The fork pins `transformers==4.51.3`, but Dramabox's Gemma encoder needs `>=4.52`. They can't share a working venv at the same time. DESIGN.md §16.6 has the details. The launcher resolves this per phase. In the Studio it limits you to one engine per session (see [Voicebank Studio](/guides/voicebank-studio/#tts-lab)).
:::

## `s4_retry.sh`

Restarts `lnvox s4` until it succeeds. Rendered beats are content-cached in `cache/tts/`, so every restart resumes where the last attempt died (see [s4](/stages/s4-tts/)).

```bash
MAX_ATTEMPTS=50 ./scripts/s4_retry.sh novel-name/volume-01
```

| Input | Default | Meaning |
|---|---|---|
| `$1` (book id) | required | Passed to `uv run lnvox s4 "$BOOK"`. Without it the script prints its usage and exits 2 |
| `MAX_ATTEMPTS` | `100` | Attempt budget |
| `RETRY_DELAY` | `5` | Seconds between attempts (the launcher's variable of the same name) |
| `LNVOX_ARTIFACTS_DIR` | `artifacts` | Where the log goes: the book's own artifacts folder, the same one `lnvox s4` writes to |
| `LNVOX_TTS_BACKEND` | (unset → dramabox) | Read by `lnvox s4` itself and selects the engine |

Behaviour:

- Every attempt appends to `$LNVOX_ARTIFACTS_DIR/<book>/_s4_full.log`, framed by `==== Attempt N @ <ISO time> ====` banners. A relative path resolves against the current directory, as it does for the CLI: run it from the repo root.
- `lnvox`'s own exit status is read from `PIPESTATUS[0]` straight after the `uv run lnvox s4 … 2>&1 | tee -a "$LOG"` pipeline (`uv run` passes the child's status through), then:

| `lnvox s4` exit | Banner | Then |
|---|---|---|
| 0 | `Completed on attempt N` | exit 0 |
| 2 | `Exit 2 on attempt N: deterministic refusal, not retrying` | exit 2 at once. lnvox uses 2 for usage and configuration refusals (e.g. staged-only knobs on the monolithic path), which a retry would only replay |
| ≥ 128 (signal: segfault, OOM kill) | `Crashed on attempt N; retrying in 5s` | sleep, retry |
| anything else (a Python exception) | `Exit N on attempt N; retrying in 5s` | sleep, retry |

- Once the budget is spent it logs `Gave up after N attempts` and exits 1.
- It passes neither `--staged` nor `--device`. `lnvox s4` auto-detects the device (MPS on Apple Silicon, else CUDA).

The launcher uses it for the two non-staged TTS paths:

```bash title="scripts/run_pipeline.sh"
    if [ "$TTS_BACKEND" = "vibevoice" ]; then
        prepare_vibevoice_env
        ...
        MAX_ATTEMPTS="$MAX_RETRIES" LNVOX_TTS_BACKEND=vibevoice ./scripts/s4_retry.sh "$BOOK_ID"
        check_s4_retry $?
    elif [ "$STAGED_TTS" -eq 1 ]; then
        prepare_tts_env
        ...
        uv run lnvox s4 "$BOOK_ID" --staged
    else
        prepare_tts_env
        banner "Stage 4: TTS (Dramabox, with auto-retry)"
        MAX_ATTEMPTS="$MAX_RETRIES" ./scripts/s4_retry.sh "$BOOK_ID"
        check_s4_retry $?
    fi
```

`check_s4_retry` stops the launcher on any non-zero status, with that status, so a render the loop gave up on is never mixed (see [launcher functions](/launcher/functions/#check_s4_retry)).

`MAX_RETRIES` comes from `--max-retries` (or env `MAX_RETRIES`, default 30). The staged Dramabox path, the launcher default with `LNVOX_S4_STAGED=1`, does **not** use this loop. Its driver runs one subprocess per phase, resumes at file level, and aborts after `LNVOX_S4_STALL_LIMIT` restarts in a row with no progress (see [staged driver](/library/tts/staged-driver/)). To get the monolithic path plus retry loop, set `LNVOX_S4_STAGED=0`. The `--staged-tts` flag can only turn staging on.

:::caution
Only exit 2 counts as deterministic. A broken environment still raises an ordinary exception (exit 1) — for example `No module named 'mamba_ssm'` after a failed wheel fetch — and crashes identically on every attempt until the budget runs out. If `_s4_full.log` shows the same traceback over and over, stop the loop and fix the environment.
:::
