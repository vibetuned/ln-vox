---
title: run_pipeline.sh
description: The one-shot launcher — every option, the environment it reads and exports, and the exact order in which it runs the stages for each mode.
sidebar:
  order: 1
  label: Options and flow
---

`scripts/run_pipeline.sh` runs the whole pipeline for one book: it prepares the venv for the LLM phase, starts an LLM server, runs every LLM stage, stops the server, swaps the venv to the TTS runtime, renders, mixes and syncs. Every stage it calls is an idempotent `lnvox` command, so re-running the same command line resumes a crashed or interrupted run instead of repeating it.

```bash
./scripts/run_pipeline.sh <series>/<volume-XX> [options]
```

The internals — each shell function, the retry wrapper and the server lifecycle — are on [Launcher functions](/launcher/functions/). The serve and setup scripts it calls are on [Helper scripts](/launcher/helper-scripts/).

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `<book_id>` (positional) | required | `<series>/<volume-NN>` (or a flat id). Chapters are read from `<novels-root>/<book_id>/`, artifacts written to `$LNVOX_ARTIFACTS_DIR/<book_id>/` (default `artifacts/`). Optional only with `--prepare-env`. |
| `--mode <m>` | `narration` | `narration` (multi-voice novel), `lecture` (single-voice non-fiction, [guide](/guides/lecture/)) or `scenario` (theater script, [guide](/guides/scenario/)). |
| `--scenario-file <path>` | — | Scenario mode: the script markdown to ingest. Omit to reuse a prior, complete ingest (`00_script.json` **and** `01_characters.json` under the book's artifacts). |
| `--no-normalize` | off | Lecture mode: skip the speech-normalize LLM pass (read text verbatim). |
| `--narrator-clip <id>` | — | Voicebank clip id for the Narrator. Optional when a prior volume already has one — it's inherited. |
| `--audit-cast <mode>` | `report` (`$LNVOX_AUDIT_CAST`) | Casting audit after voice cast: `report`, `fix` (auto-reassign) or `off`. See [voice audit](/stages/voice-audit/). |
| `--stop-after-audit` | off | Stop right after the casting audit, before s3 — the hand-casting checkpoint ([guide](/guides/casting/)). |
| `--book-title <title>` | the book id | Title of the final `.m4b`. |
| `--novels-root <path>` | `novels` | Where the chapter `.txt` folders live. Passed to both s5 (embedded pictures) and s6 (alignment), so the two always read the same folder. |
| `--vllm-url <url>` | — | Use an already-running OpenAI-compatible server at this URL; skips auto start/stop and env prep. The name is historical — it works with any backend. Without it the server URL is resolved from the environment (see [Environment](#environment)). |
| `--llm-backend <name>` | `llama` (`$LNVOX_LLM_BACKEND`) | `llama` (llama.cpp, any OS), `vllm` (Linux + CUDA) or `mlx` (Apple Silicon). |
| `--llm-model <id>` | `$LNVOX_LLM__MODEL`, else `$LNVOX_LLM_MODEL`, else per backend (below) | HF repo, GGUF repo or local path, depending on the backend. |
| `--max-model-len <N>` | the serve script's | Context window, for the serve script **and** the client's output budgets: exported as `LNVOX_LLM_MAX_LEN`, `LNVOX_LLM_MAX_MODEL_LEN` and `LNVOX_LLM__MAX_MODEL_LEN`. Must be a positive integer (exit 2 otherwise). mlx-lm has no such knob. |
| `--skip-llm` | off | Skip ingest, s1–s3 and voice cast (assume done). |
| `--skip-tts` | off | Skip s4. |
| `--remote-tts <host>` | — | Render s4 on another machine over SSH with [`lnvox remote-tts`](/guides/remote-tts/): the first run launches it there (or queues it behind another book) and exits 0; run again with `--skip-llm` to check on it, and once it's done to fetch `05_audio` and go on with s5/s6 here. Dramabox only; contradicts `--skip-tts`. |
| `--staged-tts` | on (`$LNVOX_S4_STAGED`, default `1`) | Render s4 with the staged Dramabox pipeline. Dramabox only. Set `LNVOX_S4_STAGED=0` for the monolithic path (refused on Apple Silicon — see [Apple Silicon](/guides/apple-silicon/)). |
| `--tts-backend <name>` | `dramabox` (`$LNVOX_TTS_BACKEND`) | `dramabox` (per beat → `05_audio/`) or `vibevoice` (multi-speaker sessions → `05_audio_v2/`; s5 mixes with `--v2`, s6 is skipped). |
| `--skip-mix` | off | Skip s5. |
| `--skip-sync` | off | Skip s6 / scenario-sync. |
| `--epub <path>` | `epubs/<book_id>.epub` | Source EPUB for s6. |
| `--max-retries N` | `30` (`$MAX_RETRIES`) | Restart budget for the monolithic / VibeVoice s4 retry loop (`s4_retry.sh`). |
| `--step-retries N` | `3` (`$STEP_RETRIES`) | Retry budget for every other step. Exit 2 (a deterministic refusal) and signals are never retried. |
| `--prepare-env <phase>` | — | Only prepare the venv for one phase — `llm` (per `--llm-backend`), `tts` (Dramabox) or `vibevoice` — then exit. For orchestrators that drive stages one at a time, such as the [ComfyUI nodes](/guides/comfyui/). |
| `-h`, `--help` | | Print the option list from the script header. |

Default LLM model per backend, used when neither `--llm-model` nor the environment names one. Whatever model is chosen is exported as both `LNVOX_LLM_MODEL` and `LNVOX_LLM__MODEL`:

| Backend | Serve script | Default model |
| --- | --- | --- |
| `llama` | `scripts/serve_llama.sh` | `google/gemma-4-12B-it-qat-q4_0-gguf` |
| `vllm` | `scripts/serve_vllm.sh` | `google/gemma-4-E4B-it` |
| `mlx` | `scripts/serve_mlx.sh` | `mlx-community/gemma-4-12B-it-qat-4bit` |

## Environment

Read by the launcher:

| Variable | Used for |
| --- | --- |
| `LNVOX_LLM_BACKEND`, `LNVOX_TTS_BACKEND`, `LNVOX_S4_STAGED`, `LNVOX_AUDIT_CAST` | Defaults for the matching options |
| `LNVOX_LLM__MODEL`, then `LNVOX_LLM_MODEL` | Default for `--llm-model` (same precedence as [config](/library/config/)) |
| `LNVOX_LLM__ENDPOINT`, then `LNVOX_LLM_ENDPOINT`, then `http://localhost:${LNVOX_LLM_PORT:-8000}/v1` | The LLM server URL when `--vllm-url` isn't given: the readiness probe polls it and the stages talk to it. `LNVOX_LLM_PORT` is the port the serve scripts bind, so moving it moves the probe too |
| `LNVOX_LLM_MAX_LEN`, `LNVOX_LLM_MAX_MODEL_LEN`, `LNVOX_LLM__MAX_MODEL_LEN` | Only checked for presence: any of them set means the launcher doesn't derive a context window (below) |
| `LNVOX_ARTIFACTS_DIR` | Artifacts root (default `artifacts`), the same one the `lnvox` CLI writes to. Every existence check (`00_script.json`, `01_characters.json`, `05_audio/`) and printed path goes through it |
| `LNVOX_VOICEBANK` | Only echoed in the hand-casting hint, and only when set; the `lnvox` commands read it themselves |
| `MAX_RETRIES`, `STEP_RETRIES`, `RETRY_DELAY` | Retry budgets and the delay between retries (seconds, default 5; `s4_retry.sh` honours it too) |
| `LNVOX_CAFFEINATED` | Internal guard for the macOS re-exec |

The launcher reads the process environment only, never `.env`. Keep launcher settings in exported variables.

Exported to every stage and serve script:

- `LNVOX_LLM_BACKEND` — always. `LLMClient` keys backend-specific behaviour on it (it disables Gemma's thinking channel on llama-server and mlx-lm; without it structured calls burn their budget on reasoning tokens).
- `LNVOX_LLM_MODEL` and `LNVOX_LLM__MODEL` — always, with the chosen model. vLLM validates the request's model name strictly, and the nested spelling wins in `Settings`, so a stale `LNVOX_LLM__MODEL` in the environment can't make the client name a different model than the one the server loaded.
- `LNVOX_LLM__ENDPOINT` — always, with the resolved server URL. The nested form wins in `Settings`, so the client always talks to the server the probe checked, even over a `.env` value.
- `LNVOX_LLM_MAX_LEN`, `LNVOX_LLM_MAX_MODEL_LEN` and `LNVOX_LLM__MAX_MODEL_LEN` — with `--max-model-len`. The first sizes the server; the others set the client's `max_model_len`, which [`budget_for`](/library/llm/client/) clamps output budgets against.

When the launcher starts `serve_vllm.sh` itself (`--llm-backend vllm`, no `--vllm-url`) for a model whose name contains `31B` or `27B`, and no context length is set anywhere, it sets the context to 20480 — the window `serve_vllm.sh` picks for those models — so the client doesn't assume its 65 536 default and overflow the server's window. The full variable list is on [environment variables](/reference/environment/).

## What runs, in order

```text
narration  ingest → s1 → s2 → voice cast → audit → [stop-after-audit?] → s3 → ‖ → s4 → s5 → s6
lecture    ingest → voice cast → audit → [stop?] → lecture → ‖ → s4 → s5 → s6
scenario   ingest-scenario → voice cast → scenario → ‖ → s4 → s5 → scenario-sync
                                                     ‖ = LLM server stopped, venv swapped to TTS
```

### 1. Prologue

1. **macOS: re-exec under `caffeinate -dis`** once (guarded by `LNVOX_CAFFEINATED`), so a multi-hour s4 isn't throttled or suspended by power management.
2. **Parse options**, then validate: `--llm-backend` ∈ {llama, vllm, mlx}; `--tts-backend` ∈ {dramabox, vibevoice}; an *explicit* staged request (`--staged-tts` or `LNVOX_S4_STAGED=1` set in the environment) with vibevoice is an error, while the staged *default* is silently ignored for vibevoice; `--mode` ∈ {narration, lecture, scenario}; `--max-model-len` is a positive integer; scenario mode needs `--scenario-file` or a complete prior ingest — `00_script.json` **and** `01_characters.json`. The current ingest-scenario writes `00_script.json` last, after the cast, so a crash leaves no half-written ingest; the second file catches folders from older versions (which wrote the script before the characters pass) or a hand-deleted cast. Such a run is refused with a hint to pass `--scenario-file` again (the structuring calls come back from `cache/scenario/`).
3. **Decide whether an LLM is needed at all.** A lecture run with `--no-normalize` *and* `--narrator-clip` makes zero LLM calls, so `NEED_LLM=0` and no server is started.
4. **Check the chapter folder** `<novels-root>/<book_id>` exists (not for scenario mode, `--skip-llm` or `--prepare-env`).
5. **Resolve and export** the LLM server URL, model and context window (above) and install the `cleanup` trap (stops a managed server on EXIT/INT/TERM).
6. **`--prepare-env` mode** runs the requested `prepare_*_env` function and exits here.

### 2. LLM phase (unless `--skip-llm`)

1. `prepare_llm_env` (skipped with `--vllm-url` or when `NEED_LLM=0`), then `start_vllm`.
2. Stage 0 — `lnvox ingest-scenario` (scenario, when `--scenario-file` is given; otherwise the validated prior ingest is reused) or `lnvox ingest --mode <m>` (narration / lecture). [ingest](/stages/ingest/), [ingest-scenario](/stages/ingest-scenario/)
3. Narration only: `lnvox s1` (this volume's cast only — s1 doesn't merge prior volumes' casts; recurring voices carry over at voice cast), `lnvox s2`. [s1](/stages/s1-characters/), [s2](/stages/s2-scenes/)
4. `lnvox voice cast [--narrator-clip …]`. [voice cast](/stages/voice-cast/)
5. Not scenario, audit not `off`: `lnvox voice audit [--fix] || true` — never fatal. [voice audit](/stages/voice-audit/)
6. `--stop-after-audit`: stop the server, print the Studio + re-audit commands (prefixed with `LNVOX_VOICEBANK` / `LNVOX_ARTIFACTS_DIR` only when this run had them set), **exit 0**.
7. Direction: `lnvox s3` (narration), `lnvox scenario` (scenario) or `lnvox lecture [--no-normalize]` (lecture). s3 runs **without** `--regen-profiles`: its caches are casting-aware, so only recast characters get new voice profiles, and cached chapters are re-voiced with them without new direction calls. `--regen-profiles` would force every descriptor and make s4 re-render the whole book on every run. [s3](/stages/s3-director/), [scenario](/stages/scenario/), [lecture](/stages/lecture/)
8. `stop_vllm` — frees the GPU for the TTS model.

Every LLM step runs through `run_step ensure_vllm …`: a failure is retried up to `--step-retries` times, and a server that died is relaunched before the retry. Exit status 2 — lnvox's code for a deterministic refusal, such as a mistyped `--narrator-clip` — is never retried: the launcher stops at once.

### 3. TTS phase (unless `--skip-tts`)

Sleeps 5 s (managed server only) so VRAM is actually released, then one of:

| Condition | Env prep | Command |
| --- | --- | --- |
| `--remote-tts <host>` | on the box | `uv run lnvox remote-tts <book> --host <host>` ([guide](/guides/remote-tts/)). Exit 3 — launched, queued or still rendering — ends the launcher with exit 0 and a reminder to come back; exit 0 — the render is fetched — goes on to the mix; anything else is the launcher's exit status. No 5 s wait |
| `--tts-backend vibevoice` | `prepare_vibevoice_env` | `MAX_ATTEMPTS=$MAX_RETRIES LNVOX_TTS_BACKEND=vibevoice ./scripts/s4_retry.sh <book>` |
| dramabox, staged (default) | `prepare_tts_env` | `uv run lnvox s4 <book> --staged` — no outer retry; the staged driver owns crash recovery. A failure aborts the launcher (exit 1, or 2 for a refusal) |
| dramabox, `LNVOX_S4_STAGED=0` | `prepare_tts_env` | `MAX_ATTEMPTS=$MAX_RETRIES ./scripts/s4_retry.sh <book>` |

See [Stage 4](/stages/s4-tts/). Every branch checks s4's exit status: when `s4_retry.sh` gives up (exit 1), refuses (exit 2) or is killed, the launcher prints `s4 failed (s4_retry.sh exit N) — not mixing a partial render` and exits with that status instead of mixing whatever chapters did render.

### 4. Mix (unless `--skip-mix`)

`lnvox s5 <book> --title <title> --novels-root <root>`, plus `--v2` for vibevoice and, in scenario mode, `--inter-chapter 0.75 --lead-in 0.35` (play pacing that scenario-sync reproduces). Wrapped in `run_step -` (retries, no hook). [Stage 5](/stages/s5-mix/)

### 5. Sync (unless `--skip-sync`)

| Situation | What happens |
| --- | --- |
| scenario + dramabox | `lnvox scenario-sync <book>` ([scenario-sync](/stages/scenario-sync/)) |
| scenario + vibevoice, or any vibevoice book | skipped — session WAVs have no per-line timing |
| no EPUB at `--epub` / `epubs/<book_id>.epub` | skipped with a hint |
| no `05_audio/` under the book's artifacts | skipped |
| otherwise | `lnvox s6 <book> --epub <path> --novels-root <root>` ([Stage 6](/stages/s6-sync/)) |

Finally it prints the path of the `.m4b` s5 wrote — `06_final/<title>.m4b` with `/` and `\` in the title replaced by `_`, as [`safe_filename`](/library/stages/s5-mix/) does (the default title is the book id, so `novel/volume-01` becomes `novel_volume-01.m4b`) — and lists `06_final/`.

## Recipes

```bash
# First volume: pick the narrator by ear, name the book.
./scripts/run_pipeline.sh novel/volume-01 --narrator-clip cv_051e865815e5 \
    --book-title "My Novel — Volume 1"

# Later volumes inherit the narrator and every recurring voice.
./scripts/run_pipeline.sh novel/volume-02 --book-title "My Novel — Volume 2"

# Hand-cast checkpoint, then continue (cached stages are no-ops).
./scripts/run_pipeline.sh novel/volume-01 --narrator-clip cv_… --stop-after-audit
./scripts/run_pipeline.sh novel/volume-01 --narrator-clip cv_…

# Re-render and re-mix only (after hand-editing 03_directed/).
./scripts/run_pipeline.sh novel/volume-01 --skip-llm

# After a casting edit (Studio or JSON), re-run normally: voice cast keeps the
# locked edit, s3 re-voices only the recast characters, s4 re-renders their beats.
./scripts/run_pipeline.sh novel/volume-01

# Use a server you already run elsewhere.
./scripts/run_pipeline.sh novel/volume-01 --vllm-url http://gpu-box:8000/v1 --llm-backend vllm

# Lecture with zero LLM calls.
./scripts/run_pipeline.sh nonfiction/book --mode lecture --no-normalize --narrator-clip cv_…

# Prepare the TTS venv only (what the ComfyUI TTS node does).
./scripts/run_pipeline.sh --prepare-env tts
```

:::tip[Log it]
Nothing is logged to disk except `s4_retry.sh`'s `_s4_full.log` in the book's artifacts. Wrap long runs: `./scripts/run_pipeline.sh … 2>&1 | tee logs/run.log` — the script uses `set -o pipefail`, and a stage's exit status is still what drives its retries.
:::
