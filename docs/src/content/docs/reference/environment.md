---
title: Environment variables
description: Every environment variable ln-vox reads, grouped by area — default, the file that reads it, its effect — plus the naming traps between scripts and the client.
sidebar:
  order: 2
---

Every environment variable read by `src/lnvox/`, `scripts/*.sh` and `scripts/*.py`, grouped by the component that reads it. Defaults are copied from the code. "Launcher" means `scripts/run_pipeline.sh`. Read [Pitfalls](#pitfalls) before setting LLM variables: the serve scripts and the Python client use **different names** for some of the same settings.

## LLM client (Python)

Read by `lnvox.config.Settings` (pydantic-settings, prefix `LNVOX_`, nested delimiter `__`, also loaded from `./.env`) or directly by `lnvox.llm.client`. Every `lnvox` command that calls the LLM builds a fresh `Settings()`. Details: [lnvox.config](/library/config/).

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `LNVOX_ARTIFACTS_DIR` | `artifacts` | `config.py` (`Settings.artifacts_dir`); launcher, `s4_retry.sh` | Root for `artifacts/<book_id>/`. Honoured by the `lnvox` CLI, the launcher (every existence check and printed path), `s4_retry.sh` (its log path), the Voicebank Studio (through `Settings`, so `.env` works there too) and the ComfyUI pack. The two shell scripts read the process environment only. |
| `LNVOX_LLM__ENDPOINT` | `http://localhost:8000/v1` | `config.py`; launcher | OpenAI-compatible base URL. The launcher always exports it, set to the URL it resolved: `--vllm-url`, else this variable, else `LNVOX_LLM_ENDPOINT`, else `http://localhost:${LNVOX_LLM_PORT:-8000}/v1`. Its readiness probe polls the same URL. |
| `LNVOX_LLM__MODEL` | `google/gemma-4-12B-it-qat-q4_0-gguf` | `config.py`; launcher | Request `model` field. It must match the served id on vLLM and is ignored by llama-server / mlx-lm. The launcher takes it as the default for `--llm-model` (before `LNVOX_LLM_MODEL`) and exports both spellings with the model it picked. |
| `LNVOX_LLM__API_KEY` | `EMPTY` | `config.py` | Bearer token. |
| `LNVOX_LLM__TEMPERATURE` | `0.2` | `config.py` | Base temperature. Retries after invalid JSON add 0.4 each, capped at 1.0. |
| `LNVOX_LLM__MAX_TOKENS` | `8192` | `config.py` | Output budget when a caller passes none. |
| `LNVOX_LLM__REPETITION_PENALTY` | `1.0` | `config.py` | Not 1.0 → sent as `repetition_penalty` (and `repeat_penalty` on llama). |
| `LNVOX_LLM__MAX_MODEL_LEN` | `65536` | `config.py` | Context window the client assumes when clamping budgets. The launcher exports it with `--max-model-len`, or as 20480 for a 31B/27B model on a `serve_vllm.sh` it starts (see [context length](#llm-context-length-three-names)). |
| `LNVOX_LLM__TIMEOUT_BASE_SECONDS` | `90.0` | `config.py` | Per-call timeout = base + `max_tokens` × per-token. |
| `LNVOX_LLM__TIMEOUT_SECONDS_PER_TOKEN` | `0.25` | `config.py` | Lower it on fast GPUs, raise it (0.35–0.5) on very slow hosts. |
| `LNVOX_LLM` | — | `config.py` | Whole `llm` block as JSON, e.g. `'{"endpoint":"http://h:8000/v1","max_model_len":32768}'`. |
| `LNVOX_LLM_MODEL` | — | `config.py` (`model_post_init`) | Shorthand for `llm.model`. Applied unless `LNVOX_LLM__MODEL` was set explicitly. Also read by all serve scripts, and by the launcher as the default for `--llm-model` when `LNVOX_LLM__MODEL` is unset. |
| `LNVOX_LLM_ENDPOINT` | — | `config.py` (`model_post_init`); launcher | Shorthand for `llm.endpoint`. The launcher reads it too, after `LNVOX_LLM__ENDPOINT`, when it resolves the server URL. |
| `LNVOX_LLM_API_KEY` | — | `config.py` (`model_post_init`) | Shorthand for `llm.api_key`. |
| `LNVOX_LLM_MAX_MODEL_LEN` | — | `config.py` (`model_post_init`) | Shorthand for `llm.max_model_len`, parsed with `int()`. An unparsable value is skipped without warning. The launcher exports it with `--max-model-len`. |
| `LNVOX_LLM_MAX_LEN` | — | `config.py` (`model_post_init`); serve scripts | Fallback for `llm.max_model_len` when `LNVOX_LLM_MAX_MODEL_LEN` isn't set (see [context length](#llm-context-length-three-names)). It is also the serve scripts' context window. The launcher's `--max-model-len` exports it along with the other two names. |
| `LNVOX_LLM_REPETITION_PENALTY` | — | `config.py` (`model_post_init`) | Shorthand for `llm.repetition_penalty`, parsed with `float()`. An unparsable value is skipped without warning. |
| `LNVOX_LLM_BACKEND` | unset (treated as `llama`) | `llm/client.py` (every call); launcher | `llama` or `mlx`: send `chat_template_kwargs.enable_thinking=False`. `llama` also sends `repeat_penalty`. `vllm`: leave template defaults alone. The launcher validates `llama\|vllm\|mlx` (default `llama`) and exports it. |

**Precedence** for the shorthands: an explicitly set `LNVOX_LLM__<FIELD>` (env or `.env`, even when it equals the default) wins. Then comes the shorthand from the process environment, then the shorthand from `.env`, then the default. For the context window, `LNVOX_LLM_MAX_MODEL_LEN` is tried before `LNVOX_LLM_MAX_LEN` within each source. There is no shorthand for temperature, max tokens or timeouts. See [shorthand aliases](/library/config/#shorthand-aliases-model_post_init).

`OPENAI_API_KEY` / `OPENAI_BASE_URL` have no effect, because the client passes both explicitly.

## LLM servers (`scripts/serve_*.sh`)

Each serve script is started in the background by the launcher (`nohup`), or you can run it by hand. `serve_llama.sh` also accepts `--llm-model`, `--port`, `--host`, `--max-model-len` and `--n-gpu-layers` flags, which export the matching variable.

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `LNVOX_LLM_MODEL` | llama: `google/gemma-4-12B-it-qat-q4_0-gguf` · mlx: `mlx-community/gemma-4-12B-it-qat-4bit` · vllm: `google/gemma-4-E4B-it` | `serve_llama.sh`, `serve_mlx.sh`, `serve_vllm.sh` | Model to load. For llama, an existing file path uses `-m`, anything else `-hf` (HF repo id, optional `:QUANT` suffix, auto-download). Under the launcher it is always the model the launcher picked. |
| `LNVOX_LLM_PORT` | `8000` | all three; launcher | Listen port. The Python client doesn't derive its URL from it, but the launcher does: with no `--vllm-url` and no endpoint variable, it probes (and exports to the stages) `http://localhost:${LNVOX_LLM_PORT:-8000}/v1`, so a custom port moves server, probe and client together. |
| `LNVOX_LLM_HOST` | `127.0.0.1` | `serve_llama.sh`, `serve_mlx.sh` | Bind address (`serve_vllm.sh` passes no host). The launcher's default URL always says `localhost`, whatever the bind address; set `LNVOX_LLM__ENDPOINT` for anything else. |
| `LNVOX_LLM_MAX_LEN` | llama: `65536` · vllm: `20480` if the model name contains `31B`/`27B`, else `65536` · mlx: ignored | `serve_llama.sh` (`--ctx-size`), `serve_vllm.sh` (`--max-model-len`) | Server context window. mlx-lm has no such flag, so `serve_mlx.sh` ignores it; the window comes from the model's config. The launcher exports it for `--max-model-len`, and the Python client reads it too, as a fallback for `llm.max_model_len`. |
| `LNVOX_LLM_N_GPU_LAYERS` | `999` | `serve_llama.sh` | `--n-gpu-layers`. `999` = full offload, `0` = CPU only. |
| `LNVOX_LLM_GPU_UTIL` | `0.88` if the model contains `31B`/`27B`, else `0.85` | `serve_vllm.sh` | `--gpu-memory-utilization`. |
| `LNVOX_LLM_TEMP` | `0.2` | `serve_mlx.sh` | Server-wide `--temp`. mlx-lm 0.31.3 ignores per-request temperature, so this is the effective sampling temperature on mlx. It doesn't set the client's `llm.temperature`. |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` (only for `31B`/`27B` models, only if unset) | `serve_vllm.sh` | Exported for vLLM to reduce fragmentation OOMs on memory-tight models. |

## Launcher (`run_pipeline.sh`, `s4_retry.sh`)

See [run_pipeline.sh](/launcher/run-pipeline/) and [launcher functions](/launcher/functions/) for the flags these mirror.

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `LNVOX_LLM_BACKEND` | `llama` | `run_pipeline.sh` | Default for `--llm-backend` (`llama\|vllm\|mlx`). It picks the serve script and the per-backend model default, and is exported to every stage. |
| `LNVOX_LLM__MODEL`, then `LNVOX_LLM_MODEL` | per-backend default | `run_pipeline.sh` | Default for `--llm-model`, in the same order as `config.py` (nested first). Whatever wins is exported under both names. |
| `LNVOX_LLM__ENDPOINT`, then `LNVOX_LLM_ENDPOINT`, then `LNVOX_LLM_PORT` | `http://localhost:8000/v1` | `run_pipeline.sh` | The server URL when `--vllm-url` isn't given: `${LNVOX_LLM__ENDPOINT:-${LNVOX_LLM_ENDPOINT:-http://localhost:${LNVOX_LLM_PORT:-8000}/v1}}`. The readiness probe polls it and it is exported to the stages as `LNVOX_LLM__ENDPOINT`. |
| `LNVOX_LLM_MAX_LEN`, `LNVOX_LLM_MAX_MODEL_LEN`, `LNVOX_LLM__MAX_MODEL_LEN` | unset | `run_pipeline.sh` | Only checked for presence: if any is set, the launcher doesn't apply its vLLM 31B/27B context rule (see [context length](#llm-context-length-three-names)). |
| `LNVOX_ARTIFACTS_DIR` | `artifacts` | `run_pipeline.sh`, `s4_retry.sh` | Artifacts root, the same one the `lnvox` CLI uses. The launcher runs every existence check (`00_script.json`, `01_characters.json`, `05_audio/`) and prints every path under it. `s4_retry.sh` writes `_s4_full.log` there. |
| `LNVOX_VOICEBANK`, `LNVOX_ARTIFACTS_DIR` | unset | `run_pipeline.sh` | With `--stop-after-audit`, the Studio and re-audit commands it prints are prefixed with these only when this run had them set, so a bank or root kept in `.env` isn't overridden by a spelled-out default. |
| `LNVOX_AUDIT_CAST` | `report` | `run_pipeline.sh` | Default for `--audit-cast`. `off` skips the casting audit, `fix` runs `lnvox voice audit --fix`, and anything else reports. Never fatal. Not run in scenario mode. |
| `LNVOX_S4_STAGED` | `1` | `run_pipeline.sh` | `1` runs `lnvox s4 --staged` (Dramabox staged driver, no outer retry loop). Any other value uses `s4_retry.sh` (monolithic). Setting it to `1` explicitly with `--tts-backend vibevoice` is an error. The default is silently ignored for vibevoice. |
| `LNVOX_TTS_BACKEND` | `dramabox` | `run_pipeline.sh` | Default for `--tts-backend` (`dramabox\|vibevoice`). vibevoice also switches s5 to `--v2` and skips s6 / scenario-sync. |
| `MAX_RETRIES` | `30` | `run_pipeline.sh` | Default for `--max-retries`. Passed as `MAX_ATTEMPTS` to `s4_retry.sh` (monolithic Dramabox and vibevoice paths only; the staged path ignores it). |
| `STEP_RETRIES` | `3` | `run_pipeline.sh` | Default for `--step-retries`. Total attempts per non-TTS step in `run_step`, including the first. |
| `RETRY_DELAY` | `5` | `run_pipeline.sh`, `s4_retry.sh` | Seconds `run_step` sleeps before a retry (and before the `ensure_vllm` hook), and seconds `s4_retry.sh` waits between attempts. The launcher doesn't export it, so `s4_retry.sh` sees it only when it is in your environment. |
| `LNVOX_CAFFEINATED` | unset | `run_pipeline.sh` | macOS only: if unset and `caffeinate` exists, the launcher sets it to `1` and re-execs itself under `caffeinate -dis`. Set it to any non-empty value to skip that. |
| `MAX_ATTEMPTS` | `100` | `s4_retry.sh` | How many times to run `uv run lnvox s4 <book>` (`RETRY_DELAY` seconds apart) before giving up with exit 1. The launcher sets it from `MAX_RETRIES`. |

**Exported by the launcher** (not meant to be set by you for the launcher itself):

- `LNVOX_LLM_BACKEND`: always.
- `LNVOX_LLM_MODEL` and `LNVOX_LLM__MODEL`: always, with the same value: `--llm-model`, else `$LNVOX_LLM__MODEL`, else `$LNVOX_LLM_MODEL`, else the per-backend default (llama `google/gemma-4-12B-it-qat-q4_0-gguf`, vllm `google/gemma-4-E4B-it`, mlx `mlx-community/gemma-4-12B-it-qat-4bit`). Exporting the nested name too means a stale `LNVOX_LLM__MODEL` can't make the client name a different model than the one the server loaded.
- `LNVOX_LLM__ENDPOINT`: always, with the resolved server URL (see the table above). The nested name wins in `Settings`, so the stages talk to the server the probe checked, even over a `.env` value.
- `LNVOX_LLM_MAX_LEN`, `LNVOX_LLM_MAX_MODEL_LEN` and `LNVOX_LLM__MAX_MODEL_LEN`: with `--max-model-len` (a positive integer, else exit 2), or `20480` when the launcher itself starts `serve_vllm.sh` for a model whose name contains `31B` or `27B` and none of the three is set. The first sizes the server; the other two set the client's `max_model_len`.
- `LNVOX_TTS_BACKEND=vibevoice`: inline, for the `s4_retry.sh` call on the vibevoice path.

The launcher reads the process environment only, never `.env`. Its exported model and endpoint therefore override any `.env` value the stages would otherwise read. `LNVOX_VLLM_BASE` is an internal shell variable holding the resolved URL. Setting it in the environment has no effect.

**Exit status.** `lnvox` exits 2 for a deterministic refusal, one that a retry would only repeat: a usage error, a mistyped `--narrator-clip`, an invalid `LNVOX_S4_*` knob, monolithic Dramabox on MPS. The launcher's `run_step` never retries exit 2. It stops at once with status 2, as it does with the signal's status on a signal exit (≥ 128). The staged s4 step exits the launcher with 2 on a refusal and 1 on any other failure. `s4_retry.sh` exits 0 on success, 2 at once when `lnvox s4` exits 2, and 1 once `MAX_ATTEMPTS` is spent; a crash or kill (≥ 128) is logged as `Crashed` and retried. When it fails, the launcher stops with its status rather than mixing a partial render. A missing book id is a usage error (exit 2) for both scripts.

## TTS / Stage 4

The `LNVOX_S4_*` render knobs are resolved in `src/lnvox/tts/dramabox_client.py` and appended to the TTS content-cache key through `s4_variant_tag()`. **Changing one re-renders every beat** under a new key (on CUDA/CPU; on MPS the tag follows the effective recipe, so the reverts are no-ops), and old renders stay in `cache/tts/`. Default staged renders are tagged `+enc-bf16+cap22+dec-fp32`. See [lnvox.tts.dramabox_client](/library/tts/dramabox-client/) and the [render-quality guide](/guides/render-quality/).

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `LNVOX_TTS_BACKEND` | `dramabox` | `cli.py` (`lnvox s4`) | Fallback when `--tts-backend` is omitted. `dramabox` (→ `05_audio/`) or `vibevoice` (→ `05_audio_v2/`). vibevoice with `--staged` exits 2. |
| `LNVOX_S4_ENCODER_PRECISION` | `bf16` on the staged path, `bnb4` on the monolithic path | `tts/dramabox_client.py`, `tts/staged.py`, `cli.py` | Gemma text-encoder precision: `bf16` (unquantized, about a 23 GB one-time download) or `bnb4` (4-bit, lighter, audibly worse). Any other value makes `lnvox s4` exit 2 before any work. Works on both paths; on MPS the staged path always uses `bf16`. An empty value is unset. |
| `LNVOX_S4_CHUNK_CAP` | `22` (seconds) | `tts/dramabox_client.py`, `tts/staged.py`, `cli.py` | Staged only. Max denoise-chunk duration, with target `max(6, cap − 4)`. `0`/`none`/`off` restores the chunker's 45 s (MPS keeps its forced 22/18). On the monolithic path an explicit non-`none` value makes `lnvox s4` exit 2. Non-numeric or negative values exit 2. An empty value is unset. |
| `LNVOX_S4_DECODE_DTYPE` | `fp32` | `tts/dramabox_client.py`, `cli.py` | Staged only. Vocoder decode dtype: `fp32`, or `auto` for per-device knobs (the old behaviour; MPS stays `fp32`). Any other value makes `lnvox s4` exit 2. An explicit `fp32` on the monolithic path makes `lnvox s4` exit 2. An empty value is unset. |
| `LNVOX_S4_STALL_LIMIT` | `10` | `tts/staged_driver.py` | Staged driver: abort a phase after this many consecutive subprocess restarts with no reduction in pending items. Raise it to grind through transient faults. |
| `LNVOX_S4_NO_TRIM` | unset | `tts/trim.py`, `tts/vibevoice_client.py`; set by `cli.py` | Exactly `1` disables the edge-silence trim on every render path. `lnvox s4 --no-trim` sets it in-process, and staged subprocesses inherit it. Not part of the cache-key tag. |
| `LNVOX_VIBEVOICE_MODEL` | unset | `tts/vibevoice_client.py` | VibeVoice weights. Resolution order: explicit argument → this variable → `models/VibeVoice-Large/` (if `config.json` exists) → HF id `microsoft/VibeVoice-Large`. |
| `LNVOX_VIBEVOICE_REF` | `07cb79feadd2d3fd7f47530d4c964a12857936a0` | `scripts/setup_vibevoice.sh` | Git ref of the VibeVoice community fork checked out into `external/VibeVoice`. |

`lnvox s4` picks its device from `--device`, falling back to `mps` when torch reports it and `cuda` otherwise. No environment variable is involved. The monolithic Dramabox path refuses an MPS device with exit 2, so on a Mac `LNVOX_S4_STAGED=0` stops the launcher instead of rendering.

## Voicebank

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `LNVOX_VOICEBANK` | `voicebank` (relative to the CWD) | `config.py` (`Settings.voicebank`), used by `cli._voicebank_dir` (`voice seed-cv`, `voice list`, `voice cast`, `voice audit`, `s3`, `scenario`, `s4`) | Selects the voicebank directory (`manifest.json` + `clips/`), e.g. `voicebank-fr` for a French bank (DESIGN.md §17.5). Read from the environment or `./.env`, like every `Settings` field. The launcher's stages inherit it. |
| `LNVOX_VOICEBANK` | `<repo>/voicebank` | `scripts/voicebank_studio.py` | Same, for the Studio, resolved through `Settings`, so a `.env` entry works too. `--voicebank` wins over it. When it is set nowhere, the fallback is the absolute repo path instead of the CWD. `LNVOX_ARTIFACTS_DIR` is resolved the same way for `--artifacts` (fallback `<repo>/artifacts`). |

s4 raises an error pointing at `LNVOX_VOICEBANK` when a casting references clip ids missing from the current bank (a casting made against a different bank). See the [voicebank stage](/stages/voicebank/) and [casting guide](/guides/casting/).

## ComfyUI node pack (`comfyui/`)

The [ComfyUI nodes](/guides/comfyui/) run `uv run lnvox …` and the serve scripts themselves, building the child environment the way the launcher does (`comfyui/nodes.py`, `comfyui/runtime.py`):

| Variable | Default | Effect |
| --- | --- | --- |
| `LNVOX_REPO` | the pack's parent directory | Where to find the ln-vox checkout if the Book node's `repo_path` is empty. |
| `LNVOX_VOICEBANK` | `voicebank` | Default for the Book node's `voicebank` widget. |
| `LNVOX_ARTIFACTS_DIR` | `artifacts` | Artifacts root recorded on the book. Forwarded to stages only when it isn't `artifacts`. |
| `STEP_RETRIES` | `3` | Per-stage retry budget, the same knob as the launcher's. Read once, when ComfyUI loads the pack. |

For child processes the pack sets `LNVOX_VOICEBANK`, `LNVOX_LLM_BACKEND`, `LNVOX_LLM_MODEL`, `LNVOX_LLM__ENDPOINT`, and **both** `LNVOX_LLM_MAX_LEN` and `LNVOX_LLM_MAX_MODEL_LEN` (the client would pick up either; see [context length](#llm-context-length-three-names)). A serve script it starts also gets `LNVOX_LLM_PORT`, taken from the endpoint's port. It also sets `LNVOX_CAFFEINATED=1` for `--prepare-env` runs and `LNVOX_S4_STAGED=0` when the Run Pipeline node's staged TTS is switched off. It removes `VIRTUAL_ENV`, `PYTHONHOME`, `PYTHONPATH` and `UV_PROJECT_ENVIRONMENT` so `uv` resolves the ln-vox `.venv` instead of ComfyUI's interpreter.

Its retries follow the launcher's rules: exit 2 is never retried (the node fails with `refused (exit 2); retrying would not help`), and neither is a signal death, except in the TTS node's monolithic / VibeVoice render loop, which restarts through a SIGKILL as `s4_retry.sh` does. The delay between attempts is a fixed 5 s; the pack doesn't read `RETRY_DELAY`.

## Third-party variables

The project code reads no `HF_*` variables. The downloads it triggers go through upstream tools that honour their usual variables (`HF_TOKEN` for gated or private repos, `HF_HOME` for the cache location):

- `serve_llama.sh` → `llama-server -hf <repo>` (GGUF download).
- `serve_vllm.sh` → vLLM model download.
- `serve_mlx.sh` → `mlx_lm server --model <repo>`.
- `tts/dramabox_client.resolve_gemma_root` → `huggingface_hub.snapshot_download` of the unquantized Gemma encoder whenever 4-bit quantization is off (bf16 encoder precision, or MPS).
- `setup_vibevoice.sh --weights-hf` → `hf download microsoft/VibeVoice-Large`. The VibeVoice client's HF-id fallback also downloads into the HF cache.

## Pitfalls

### LLM context length: three names

| Name | Who reads it |
| --- | --- |
| `LNVOX_LLM_MAX_LEN` | the serve scripts (server context window; `serve_mlx.sh` ignores it), and the Python client as its last fallback |
| `LNVOX_LLM_MAX_MODEL_LEN` | the Python client (shorthand for `LLMConfig.max_model_len`, default 65536), tried before `LNVOX_LLM_MAX_LEN` |
| `LNVOX_LLM__MAX_MODEL_LEN` | the Python client (the nested field itself, which wins whenever it is set) |

The launcher's `--max-model-len N` exports all three, so the server and the client's budget clamp ([`budget_for`](/library/llm/client/#budget_for)) use the same value, whatever your shell held before. Without the flag, the launcher mirrors `serve_vllm.sh`'s default: when it starts `serve_vllm.sh` itself for a model whose name contains `31B` or `27B`, and none of the three names is set, it exports `20480` under all three and prints `Using LLM context: 20480 tokens`. Cases that still leave the client guessing:

- **A server the launcher didn't size.** A serve script you started by hand, or an external `--vllm-url` server. `serve_vllm.sh`'s built-in 20480 for 31B/27B models never leaves that script, so the client keeps assuming 65536. Its budgets then don't clamp, and large calls fail with HTTP 400 "maximum context length". Export `LNVOX_LLM_MAX_LEN=20480` for both the script and the stages, or pass `--max-model-len` to the launcher.
- **One name set by hand, without `--max-model-len`.** Any of the three names switches the launcher's 20480 rule off, so exporting only `LNVOX_LLM_MAX_MODEL_LEN` leaves `serve_vllm.sh` on its own default while the client uses your value. Between the client's names, the nested one wins, then `LNVOX_LLM_MAX_MODEL_LEN`, then `LNVOX_LLM_MAX_LEN`, and the process environment beats `.env`. Prefer `--max-model-len`, which sets all three.

```bash
# Launcher-managed vLLM: the 20480 window of a 31B model reaches the client on its own
./scripts/run_pipeline.sh toaru/volume-01 --llm-backend vllm \
    --llm-model nvidia/Gemma-4-31B-IT-NVFP4
# External server: tell the client its window
./scripts/run_pipeline.sh toaru/volume-01 --llm-backend vllm \
    --vllm-url http://gpu-box:8000/v1 \
    --llm-model nvidia/Gemma-4-31B-IT-NVFP4 --max-model-len 20480
```

A server context *larger* than the client assumes is harmless; budgets are just clamped more than necessary.

### One model name for server and client

Under the launcher the served model and the request's `model` field always agree. The launcher takes `--llm-model`, else `$LNVOX_LLM__MODEL`, else `$LNVOX_LLM_MODEL`, else the backend's default, and exports the result under both names: the serve scripts read `LNVOX_LLM_MODEL`, and the client ranks the nested `LNVOX_LLM__MODEL` first. It reads both from the process environment only, and its export overrides a model line in `.env`. Outside the launcher, a nested `LNVOX_LLM__MODEL` that differs from the serve script's `LNVOX_LLM_MODEL` makes the client name a model the server didn't load, and vLLM rejects it.

### Running stages by hand

Outside the launcher nothing is exported for you:

- Export `LNVOX_LLM_BACKEND=vllm` (or `mlx`) when the server isn't llama-server. Unset means `llama`.
- Against `serve_vllm.sh`, export the same `LNVOX_LLM_MODEL` for both the server and the stages. The script's default (`google/gemma-4-E4B-it`) differs from the client's default (`google/gemma-4-12B-it-qat-q4_0-gguf`), and vLLM rejects an unknown model name.
- Against a serve script with a smaller window than 65536, export `LNVOX_LLM_MAX_LEN` for both (see [context length](#llm-context-length-three-names)).
- `lnvox s4` without `--staged` runs the **monolithic** path. `LNVOX_S4_STAGED` is only read by the launcher.

### Port and endpoint

The Python client never derives its URL from `LNVOX_LLM_PORT`. Run by hand, it uses `LNVOX_LLM__ENDPOINT` / `LNVOX_LLM_ENDPOINT`, else `http://localhost:8000/v1`, so a stage pointed at a serve script on another port needs `LNVOX_LLM_ENDPOINT=http://localhost:<port>/v1`. The launcher does derive it: with no `--vllm-url` and neither endpoint variable set, it probes `http://localhost:${LNVOX_LLM_PORT:-8000}/v1` and exports that URL to the stages as `LNVOX_LLM__ENDPOINT`, so a custom port moves the server, the probe and the client together. The derived URL always says `localhost`; `LNVOX_LLM_HOST` only changes where the server binds.

### Other traps

- **What `.env` covers.** `./.env` is read by `Settings` only: the field names (`LNVOX_ARTIFACTS_DIR`, `LNVOX_VOICEBANK`, `LNVOX_LLM__*`, `LNVOX_LLM`) and the [LLM shorthands](#llm-client-python). The Voicebank Studio resolves `LNVOX_VOICEBANK` and `LNVOX_ARTIFACTS_DIR` through `Settings`, so `.env` works there too. Everything else on this page (`LNVOX_LLM_BACKEND`, `LNVOX_TTS_BACKEND`, `LNVOX_S4_*`, the serve-script and launcher variables, and the ComfyUI pack's defaults) is read from the process environment only, so a `.env` line has no effect on it.
- **Generic retry names.** `MAX_RETRIES`, `STEP_RETRIES`, `RETRY_DELAY` and `MAX_ATTEMPTS` have no `LNVOX_` prefix, so a value exported for some other tool changes the launcher's retry budgets.
- **`LNVOX_ARTIFACTS_DIR` in `.env` only.** The launcher and `s4_retry.sh` honour `LNVOX_ARTIFACTS_DIR`, but only from the process environment. Kept in `.env` alone, it moves where the `lnvox` stages write while the launcher's existence checks (`00_script.json`, `01_characters.json`, `05_audio/`), its printed paths and `s4_retry.sh`'s log still use `artifacts/`. Export it instead.
- **Render knobs re-key the cache.** Toggling `LNVOX_S4_ENCODER_PRECISION`, `LNVOX_S4_CHUNK_CAP` or `LNVOX_S4_DECODE_DTYPE` between runs renders the whole book again under new cache keys — except on MPS, where the encoder is always bf16, decode always fp32 and `LNVOX_S4_CHUNK_CAP=none` still means the 22 s cap, so only an explicit different cap value re-keys.
