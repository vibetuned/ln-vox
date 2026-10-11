---
title: Launcher functions
description: Every shell function in scripts/run_pipeline.sh, and every lnvox command it runs mapped to the Python functions that do the work.
sidebar:
  order: 2
---

This page documents the internals of [`scripts/run_pipeline.sh`](/launcher/run-pipeline/) function by function, then maps each `lnvox` command the launcher runs to the library code behind it. Read it when a run fails between stages (env swap, server lifecycle, retries) or when you want to drive the stages yourself — the [ComfyUI nodes](/guides/comfyui/) re-implement exactly this contract.

## Shell functions

The script defines 14 functions. They share a handful of globals set during option parsing: `OS` (`uname -s`), `IS_WINDOWS` (1 under Git Bash / MSYS2 / Cygwin, which also exports `PYTHONUTF8=1`), `LLM_BACKEND`, `VLLM_URL`, `LNVOX_VLLM_BASE` (the server URL: `--vllm-url`, else `$LNVOX_LLM__ENDPOINT`, else `$LNVOX_LLM_ENDPOINT`, else `http://localhost:${LNVOX_LLM_PORT:-8000}/v1` — also exported to the stages as `LNVOX_LLM__ENDPOINT`), `BOOK_ART` (`${LNVOX_ARTIFACTS_DIR:-artifacts}/<book_id>`), `VLLM_PID` / `VLLM_LOG` (the managed server), `STEP_RETRIES` and `RETRY_DELAY`.

### `usage`

```bash
usage [exit_code]
```

Prints the script's header comment — everything from line 2 up to the `# LLM lifecycle` notes, with the leading `# ` stripped — and exits with `exit_code` (default 0). Called by `-h/--help` (0) and by every validation error (2). Because it prints up to a marker rather than a fixed line range, new options documented in the header always appear in `--help`.

### `banner`

```bash
banner "<text>"
```

Prints a blank line and a `####`-framed `## <text>` heading. Every phase and stage announces itself this way, which makes a long `tee`'d log easy to scan (`grep '^## ' run.log`).

### `run_step`

```bash
run_step <hook|-> "<description>" <command> [args...]
```

The retry wrapper around every non-TTS stage.

1. Runs `<command> [args...]`. Exit 0 → return 0.
2. Exit status ≥ 128 (killed by a signal — Ctrl-C, `kill`, the OOM killer) → print `terminated by signal` and **exit the whole launcher** with that status. Retrying would swallow an intentional stop.
3. Exit status 2 (lnvox's deterministic refusal: a usage error, a mistyped `--narrator-clip`, a bad knob) → print `refused its input (exit 2) — not retrying` and exit 2. A retry would replay the same refusal.
4. Attempts used up (`STEP_RETRIES`, default 3) → print `failed after N attempt(s)` and exit with the command's status.
5. Otherwise print `WARN: '<description>' failed (exit N). Retry i/N-1 in Ks…`, sleep `RETRY_DELAY` seconds (default 5), run `<hook>` unless it is `-`, and loop.

Retries are cheap because every stage is idempotent and caches per chapter / beat: a retry resumes at the first unfinished unit. LLM stages pass `ensure_vllm` as the hook, since the most common repeated failure is a crashed LLM server; ingest, s5, s6 and scenario-sync pass `-`. The casting audit is deliberately **not** wrapped — it runs as `lnvox voice audit … || true` because it exits non-zero to *report* collisions.

### `prepare_llm_env`

Makes the venv right for the LLM phase, per `LLM_BACKEND`:

| Backend | What it does |
| --- | --- |
| `mlx` | If `.venv` exists but `import mlx_lm` fails, deletes `.venv`. Then `uv sync --extra mlx --extra voice --extra tts`. |
| `llama` | `uv sync --extra voice --extra tts` (llama-server is a native binary, no Python LLM deps), then checks `llama-server` is on `PATH` and exits 1 with install hints if not. |
| `vllm` | If `.venv` exists but `import torch, vllm` fails (a CUDA/NCCL ABI mismatch left by a botched install), deletes `.venv`. Then `uv sync --extra serve --extra voice --extra tts`. |

On Windows only `llama` is accepted (exit 2 for `vllm` / `mlx`, which have no Windows wheels). Skipped when `--vllm-url` points at an external server or when no LLM is needed. Note that `uv sync` is exact: it **removes** the Dramabox overlay installed by `prepare_tts_env`, which is why the TTS phase re-installs it every run.

### `install_mamba_kernel_wheels`

```bash
install_mamba_kernel_wheels <abi-tag> <arch>     # e.g. cu12torch2.8 x86_64
```

Installs prebuilt `causal-conv1d` (`1.6.2.post1`) and `mamba-ssm` (`2.3.1`) wheels from their GitHub releases, keyed to the venv's Python tag (`cpXY`), the torch/CUDA ABI tag and the architecture. Both packages ship only sdists on PyPI; building them in uv's isolated build env links against the wrong torch, the `.so` fails to import, and RE-USE silently falls back to a kernel-free path that denoises 5–10× slower.

It first tries `uv pip install --offline --no-deps` (the uv cache): the LLM phase's `uv sync` prunes these wheels before every TTS phase, and uv revalidates a direct URL over the network even on a cache hit, so a GitHub hiccup used to leave s4 crash-looping on `No module named 'mamba_ssm'`. Only on a cache miss does it download.

### `install_dramabox_reqs`

Installs `external/DramaBox/requirements.txt` with platform-aware torch handling (mirrors `scripts/setup_dramabox.sh`). Returns 1 with a hint if the requirements file is missing.

| Platform | Handling |
| --- | --- |
| Darwin (checked first — Apple Silicon also reports `arm64`) | Strip `torch`, `torchaudio`, `torchvision`, `bitsandbytes` (no macOS wheel; DramaboxClient disables 4-bit on MPS), install the rest, then `torch>=2.10,<2.12` + `torchaudio>=2.10,<2.12` (MPS-capable PyPI wheels). |
| Windows (`IS_WINDOWS`, checked next — Git Bash reports `x86_64`) | Install Dramabox's own `torch` / `torchaudio` pins from the cu128 index first (PyPI's Windows wheels are CPU-only), then the requirements minus torch* and the mamba packages. No kernel wheels exist for Windows: s4 renders without RE-USE denoising. |
| `x86_64` | Strip `mamba-ssm` and `causal-conv1d`, install the rest (keeping Dramabox's `torch==2.8.0` pin), then `install_mamba_kernel_wheels cu12torch2.8 x86_64`. |
| `aarch64` / `arm64` (e.g. DGX Spark) | Strip torch* and the mamba packages (Dramabox's torch 2.8 has no aarch64 CUDA wheel), install the rest, then `torch==2.10.*` / `torchaudio==2.10.*` from the cu130 index, then `install_mamba_kernel_wheels cu13torch2.10 aarch64`. Pinned to 2.10 because that's the newest torch the upstream kernel wheels cover. |
| anything else | Warn and install the requirements verbatim. |

### `prepare_tts_env`

Makes the venv right for Dramabox:

1. `uv sync --extra voice --extra tts`, then `install_dramabox_reqs`.
2. `uv pip install "transformers>=4.52"` — a prior VibeVoice prep pins `transformers==4.51.3`, which breaks Dramabox's Gemma encoder, and Dramabox's own `>=4.45` spec won't force the upgrade back.
3. Sanity check in the venv: MPS available on Darwin; elsewhere CUDA available **and**, except on Windows, `causal_conv1d_cuda` / `selective_scan_cuda` importable (a failed kernel-wheel fetch is invisible to a plain CUDA probe). On failure it prints the torch version and device flags and exits 1, pointing at `scripts/setup_dramabox.sh`.

### `prepare_vibevoice_env`

1. `uv sync --extra voice --extra tts`.
2. Requires `external/VibeVoice` (exit 1 → run `scripts/setup_vibevoice.sh`), then `uv pip install -e external/VibeVoice` (which pins `transformers==4.51.3`).
3. The same MPS / CUDA sanity check as `prepare_tts_env`, without the mamba kernel imports.

### `vllm_ready`

```bash
curl -sf "${LNVOX_VLLM_BASE%/v1}/v1/models"
```

True when the OpenAI-compatible endpoint answers `/v1/models`. Works for every backend — the name is historical. `LNVOX_VLLM_BASE` is the same URL the stages receive as `LNVOX_LLM__ENDPOINT`, and its default follows `LNVOX_LLM_PORT`, the port the serve scripts bind — so a moved port moves the probe and the client with it.

### `start_vllm`

Brings up the LLM server (any backend):

1. With `--vllm-url`: exit 1 if it doesn't answer, else use it.
2. Already answering (a server you started yourself at that URL) → reuse it, don't manage it.
3. Otherwise launch the backend's serve script — `serve_vllm.sh`, `serve_mlx.sh` or `serve_llama.sh` — with `nohup` in the background, logging to a `mktemp` file (`lnvox_llm.XXXXXX.log`, path printed), and record `VLLM_PID`.
4. Poll `vllm_ready` every 3 s for up to 600 s. If the process dies first, print the last 30 log lines and exit 1; on timeout, print them, `stop_vllm`, exit 1.

Model downloads happen inside the serve script on first use, so the first start of a new model can take most of that 600 s budget.

### `stop_vllm`

If there is a managed `VLLM_PID` still alive: `kill` (SIGTERM), wait up to 30 s, then `kill -9`. Clears `VLLM_PID`. A server the launcher didn't start (external URL or pre-existing) is never touched.

### `ensure_vllm`

The pre-retry hook for LLM steps. No-op for `--vllm-url` or when `vllm_ready` succeeds; otherwise clears `VLLM_PID` (the old process is gone) and calls `start_vllm`, which hard-exits if a fresh launch never becomes ready — a server that won't start is unrecoverable.

### `check_s4_retry`

```bash
check_s4_retry <exit_status>
```

Called right after each `s4_retry.sh` run (the monolithic Dramabox and VibeVoice paths). Any non-zero status — the retry budget spent (1), a deterministic refusal (2), a signal — prints `s4 failed (s4_retry.sh exit N) — not mixing a partial render. See <book>/_s4_full.log.` and exits the launcher with that status, so s5 never mixes a partial `05_audio/` into a short `.m4b` that looks like success. The staged path checks `lnvox s4 --staged` inline the same way (exit 2 for a refusal, else 1).

### `cleanup`

Installed with `trap cleanup EXIT INT TERM`; calls `stop_vllm`, so a Ctrl-C or a failed stage never leaves a managed server holding the GPU.

## The commands it runs

Each `uv run lnvox …` line in the launcher, the CLI function that handles it (in `src/lnvox/cli.py`) and the library functions that do the work:

| Launcher step | CLI function | Library call chain |
| --- | --- | --- |
| `lnvox ingest <dir> --book-id <id> --mode <m>` | `ingest` | [`ingest_folder`](/library/ingest/text/) → `write_jsonl`; copies `.epub_meta.json` → `00_book_meta.json`; lecture writes the empty cast stub |
| `lnvox ingest-scenario <md> --id <id>` | `ingest_scenario` | [`ingest.scenario.run`](/library/ingest/scenario/) |
| `lnvox s1 <id>` | `stage1` | [`find_prior_volumes`](/library/series/) → [`s1_characters.run`](/library/stages/s1-characters/) |
| `lnvox s2 <id>` | `stage2` | [`s2_scenes.run`](/library/stages/s2-scenes/) |
| `lnvox voice cast <id> [--narrator-clip]` | `voice_cast` | [`manifest.load`](/library/voices/manifest/) → [`PriorCastIndex`](/library/voices/matcher/) (own assignments, then every prior volume) → [`cast_book`](/library/voices/matcher/) |
| `lnvox voice audit <id> [--fix]` | `voice_audit` | [`load_acoustics`](/library/voices/acoustics/) → [`scene_cooccurrence`, `find_collisions`, `repair_collisions`](/library/voices/audit/) |
| `lnvox s3 <id>` | `stage3` | [`s3_director.run`](/library/stages/s3-director/) with the casting and voicebank. No `--regen-profiles`: the caches are casting-aware, and the flag would force every descriptor (a whole-book re-render in s4) |
| `lnvox scenario <id>` | `stage_scenario` | [`stages.scenario.run`](/library/stages/scenario/) |
| `lnvox lecture <id> [--no-normalize]` | `stage_lecture` | [`narrator_descriptor`, `lecture.run`](/library/stages/lecture/) |
| `lnvox s4 <id> --staged` | `stage4` | [`staged_driver.run_staged`](/library/tts/staged-driver/) → subprocesses `lnvox s4-phase <phase> <id>` → [`staged.run_phase`](/library/tts/staged/) for refs → ctx → denoise → decode |
| `s4_retry.sh` → `lnvox s4 <id>` | `stage4` | [`s4_tts.run`](/library/stages/s4-tts/) with a [`DramaboxClient`](/library/tts/dramabox-client/) factory; model version tagged by `s4_variant_tag` |
| `s4_retry.sh` with `LNVOX_TTS_BACKEND=vibevoice` | `stage4` | [`s4_vibevoice.run`](/library/stages/s4-vibevoice/) with a [`VibeVoiceClient`](/library/tts/vibevoice-client/) factory |
| `lnvox s5 <id> --title … --novels-root … [--v2]` | `stage5` | [`resolve_book_images`, `mix`](/library/stages/s5-mix/) |
| `lnvox s6 <id> --epub … --novels-root …` | `stage6` | [`s6_sync.run`](/library/stages/s6-sync/) |
| `lnvox scenario-sync <id>` | `stage_scenario_sync` | [`scenario_sync.run`](/library/stages/scenario-sync/) |

Every CLI function resolves the book directory as `Settings().artifacts_dir / <id>` and the voicebank as `$LNVOX_VOICEBANK` (default `voicebank`); LLM-calling ones construct an [`LLMClient`](/library/llm/client/) from [`Settings`](/library/config/). The complete option list for each command is in the [CLI reference](/reference/cli/).
