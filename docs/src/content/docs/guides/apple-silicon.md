---
title: Apple Silicon
description: Running ln-vox end-to-end on an M-series Mac — setup, LLM backends, the MPS dtype map and 22 s chunk cap, memory sizing, throughput, and known issues.
sidebar:
  order: 7
---

Linux + CUDA is the primary target. Apple Silicon is a supported second target: the pipeline runs end-to-end on an M-series Mac, with LLM stages on llama.cpp or mlx-lm and Dramabox s4 on MPS. It was validated on an M4 Max / 64 GB, rendering Alice ch. I–II into a chaptered, loudness-checked 35-minute `.m4b` that passed a listening check. The contract is DESIGN.md §11. The full validation log is `MAC_TEST_REPORT.md`, and its findings are summarised [below](#validation-findings).

## Setup

```bash
# 1. Tooling
brew install ffmpeg          # s5 mix
brew install llama.cpp       # llama-server: the default LLM backend, same as Linux

# 2. Python deps (the mlx extra adds the Apple-native LLM backend)
uv sync --extra mlx --extra voice --extra tts

# 3. Dramabox: the Darwin branch installs MPS-capable torch and drops bitsandbytes
./scripts/setup_dramabox.sh

# 4. Voicebank: copy it from your main machine instead of re-seeding
#    (seeding needs the ~96 GB Common Voice tarball)
rsync -a <linux-box>:projects/ln-vox/voicebank/ voicebank/
```

On Darwin, `setup_dramabox.sh` removes `torch`, `torchaudio`, `torchvision` and `bitsandbytes` (CUDA-only, no macOS wheel) from Dramabox's requirements. It installs the rest, then `torch>=2.10,<2.12` and `torchaudio>=2.10,<2.12` from PyPI, and finally checks `torch.backends.mps.is_available()`. It exits 1 if MPS isn't available, which is what happens on an Intel Mac. See [helper scripts](/launcher/helper-scripts/#setup_dramaboxsh) for the per-platform table.

Then run the launcher as on Linux:

```bash
./scripts/run_pipeline.sh novel-name/volume-01 \
    --narrator-clip cv_… --book-title "…"                 # llama.cpp (default)

./scripts/run_pipeline.sh novel-name/volume-01 \
    --llm-backend mlx \
    --narrator-clip cv_… --book-title "…"                 # Apple-native mlx-lm
```

## What the launcher does differently on Darwin

`scripts/run_pipeline.sh` checks `uname -s` in four places:

**1. `caffeinate` re-exec.** macOS power management throttles and sleeps multi-hour renders. In one validation run the machine slept mid-render and 4 h of compute took 8.5 h of wall clock. The launcher re-executes itself once under `caffeinate -dis`, guarded by an env var:

```bash title="scripts/run_pipeline.sh"
if [ "$OS" = "Darwin" ] && [ -z "${LNVOX_CAFFEINATED:-}" ] && command -v caffeinate >/dev/null 2>&1; then
    export LNVOX_CAFFEINATED=1
    exec caffeinate -dis "$0" "$@"
fi
```

This covers only the launcher. If you run `lnvox s4` by hand, wrap it yourself (`caffeinate -dis uv run lnvox s4 …`) or attach to a running job with `caffeinate -w <pid>`.

**2. `prepare_llm_env`, mlx branch.** When `--llm-backend mlx` is set, the venv is recreated if `import mlx_lm` fails, then `uv sync --extra mlx --extra voice --extra tts` runs. mlx-lm brings its own MLX runtime, so there's no torch ABI juggling on this side. The default `llama` branch only syncs `voice` + `tts` and checks that `llama-server` is on `PATH`.

**3. `install_dramabox_reqs`, Darwin branch.** This is the same filter as `setup_dramabox.sh` (strip `torch*` and `bitsandbytes`, then install `torch>=2.10,<2.12` / `torchaudio>=2.10,<2.12`). It runs at the start of every TTS phase, because the LLM phase's `uv sync` removes Dramabox's packages, which uv does not track.

**4. MPS sanity check.** After preparing the TTS env, `prepare_tts_env` (and `prepare_vibevoice_env`) check `torch.backends.mps.is_available()` in place of the CUDA and mamba-kernel imports. If the check fails, the launcher stops with an error instead of starting s4.

There is no device flag. `lnvox s4` auto-detects `mps` via `torch.backends.mps.is_available()` (`_default_tts_device` in `src/lnvox/cli.py`), and the staged driver passes that device on to each phase subprocess. The launcher's default is the staged path (`LNVOX_S4_STAGED=1`), which is the only s4 path that works on MPS.

## LLM backends

| Backend | Script | Default model | Notes |
|---|---|---|---|
| `llama` (default) | `serve_llama.sh` | `google/gemma-4-12B-it-qat-q4_0-gguf` | Native `llama-server` from brew. Same client path as on Linux |
| `mlx` | `serve_mlx.sh` | `mlx-community/gemma-4-12B-it-qat-4bit` (~7 GB) | Apple-native. Needs the git-pinned mlx-lm |

The mlx default depends on an unreleased mlx-lm fix. Every `mlx-community` Gemma 4 **12B** conversion declares `model_type: gemma4_unified`, which no PyPI mlx-lm release (up to 0.31.3) can load. The fix is ml-explore/mlx-lm#1349, merged but unreleased. Git main still reports version 0.31.3, so `pyproject.toml` pins the commit:

```toml title="pyproject.toml"
mlx-lm = { git = "https://github.com/ml-explore/mlx-lm", rev = "8239c72de5a0e42c539e30489021db73c7fe258c" }
```

Keep the pin in `[tool.uv.sources]`. A `uv pip install git+…` would be undone by the launcher's next `uv sync`. Drop the pin at the first mlx-lm release after 0.31.3.

Other mlx models (set with `--llm-model` or `LNVOX_LLM_MODEL`):

- `mlx-community/gemma-4-31B-it-qat-4bit`: loads on any mlx-lm and needs a 32 GB+ Mac, but it's far too slow for real books. s2 took 33 min on one chapter.
- `mlx-community/gemma-4-E4B-it-4bit`: fits 16 GB Macs, but it's structurally unreliable. It produced degenerate `{}` and empty-beat output on s1/s2 (F3/F6).

What the mlx backend lacks compared to vLLM (DESIGN.md §11.2, accepted as-is): no `guided_json`, no `repetition_penalty`, no prefix caching. mlx-lm 0.31.3 also **ignores per-request sampling** and is deterministic for a given prompt at any temperature (F5). Two things make retries work anyway. `serve_mlx.sh` serves with a real `--temp` (`LNVOX_LLM_TEMP`, default 0.2). And [`LLMClient.structured`](/library/llm/client/) appends the previous validation error to the prompt on each retry, so the retry prompt differs from the one that failed. The client also sends `chat_template_kwargs: {"enable_thinking": false}` for both llama and mlx. Without it, Gemma 4's template spends the whole token budget in the reasoning channel (F2).

If you run LLM stages ad hoc instead of through the launcher, `export LNVOX_LLM_BACKEND=mlx` first. The launcher exports it for you. If it's unset, the client treats the backend as llama.

Measured LLM phase on the M4 Max (12B QAT, 3 short chapters): s1 ~70 s, s2 ~11 min, voice cast ~1.5 min, s3 ~2.5 min, about 15 min in total with no stage failures.

## TTS on MPS: the dtype map

Dramabox has no MPS-specific code, and DESIGN.md §11.3 forbids patching it. Everything Mac-specific lives in the wrapper and planner (`src/lnvox/tts/staged.py`, `src/lnvox/tts/dramabox_client.py`). The staged s4 path assigns a dtype per component on MPS:

| Component (staged phase) | MPS dtype | Why |
|---|---|---|
| Audio VAE / RE-USE refs (`refs`) | fp16 | Works as-is (`_knobs()`) |
| Gemma prompt encoder (`ctx`) | **bf16**, unquantized checkpoint | fp16 Gemma returns **100% NaN** encodings (F10). bitsandbytes 4-bit is CUDA-only, and the bnb checkpoint on disk is pre-quantized and can't be loaded without it (F8), so `resolve_gemma_root()` fetches `unsloth/gemma-3-12b-it` (~23–24 GB, one-time) |
| DiT (`denoise`) | fp16 | Works as-is, provided chunks stay short (F11, below) |
| Vocoder / audio decoder (`decode`) | **fp32** | fp16 fails with `Input type (float) and bias type (c10::Half)`, because an MPS op upcasts an intermediate that then meets fp16 weights (F9) |

`bnb_4bit` and `torch.compile` are both off on MPS (`_knobs()` returns `(fp16, False, False)`).

:::danger[F10 renders silent audio with exit code 0]
The fp16 encoder failure raised no error. s4 "succeeded", s5 produced a valid `.m4b` with correct chapters, and every sample was digital zero. The content-hash cache then kept all 136 silent WAVs. The only visible signs were the file size (1.1 MB for "36 minutes") and `volumedetect` reading −91 dB. Whenever you change anything on the Mac TTS path, check the output:

```bash
ls -la artifacts/<book>/06_final/
ffmpeg -i "artifacts/<book>/06_final/<title>.m4b" -af volumedetect -f null - 2>&1 | grep -E 'mean_volume|max_volume'
```

A healthy render is roughly 45–50 MB per hour of audio at the default 96 kbps (the validated Alice build was 25.7 MB for 35 min). Then listen to it.
:::

### The 22 s chunk cap (F11)

At about 2.6 M elements, the DiT's (T, T) self-attention mask triggers a torch-MPS boolean-indexing bug. That happens on beats longer than roughly 25 s. Two boolean-index operations over the same mask return different element counts, and DramaBox crashes with `shape mismatch: value tensor of shape [N] cannot be broadcast to indexing result of shape [M]`. The planner avoids it in two layers:

1. On MPS, `build_plan` sets `max_chunk_duration = 22.0` and `target_chunk_duration = 18.0`. That keeps the mask at about 1.3 M elements, a 2× margin, and chunks also stay under DramaBox's 20.5 s silence-prior path.
2. `_force_split_oversize()` splits at word boundaries as a last resort, repeating the `(voice direction)` header on each piece. DramaBox's `chunk_prompt_for_duration` only splits on sentence boundaries and returns one long sentence whole. The decode phase crossfades the seams.

The same recipe then won a blind A/B on CUDA: no audible artifacts, and about 20% faster denoise, because DiT attention is quadratic in chunk length. It became the staged-path default on every platform on 2026-08-04, as bf16 encoder + 22 s cap + fp32 vocoder. `LNVOX_S4_CHUNK_CAP=none` restores the 45 s chunker elsewhere, but **not on MPS**: the 22/18 caps stay because the kernel bug is a hard failure. An explicit numeric cap still wins. See [render quality](/guides/render-quality/) and [staged](/library/tts/staged/).

Staged item ids embed a hash of the chunk text (`{cache_key}_cNN-<sha8>`). Changing the chunk policy therefore invalidates every derived intermediate automatically, so never hand-purge `_staged/` files selectively (F12).

## Memory sizing

- **36 GB+ unified memory recommended** (DESIGN.md §11.3). The bf16 encoder alone is about 24 GB. The staged path loads one model per phase and gives each the whole memory pool, which is why the unquantized encoder fits at all.
- The launcher stops the LLM server before s4. Both phases want most of the memory, so don't run them at the same time.
- The Studio's TTS Lab on MPS (`StagedLabEngine`) keeps encoder, DiT and VAEs all loaded at once: about 32 GB resident (23 GB encoder + 6 GB DiT + small VAEs). See the [Voicebank Studio](/guides/voicebank-studio/#tts-lab).
- Disk: the one-time downloads are ~15 GB of Dramabox weights plus the ~23 GB unquantized encoder. mlx 12B adds ~7 GB and the llama GGUF ~8 GB.

## Throughput

Measured on an M4 Max, staged s4, final config:

| Phase | Rate |
|---|---|
| ctx (bf16 Gemma encode) | ~2 s per chunk |
| denoise (fp16 DiT, ≤22 s chunks) | ~69 s per chunk, about **3.6× real time** |
| decode (fp32 vocoder) | ~3 s per chunk |
| end to end | **~4 h of render per hour of audio** |

That beats DESIGN.md §11.3's 5–10× estimate, but only if the machine stays awake. Hence `caffeinate`. The s4 content cache makes interrupted runs resumable, so a multi-day book can be stopped and restarted.

## Validation findings

From `MAC_TEST_REPORT.md` (branch `mac-mlx-validation`). All fixes are small and apply only on Mac, except where noted.

| Id | Symptom | Fix |
|---|---|---|
| F1 | `python -m mlx_lm.server` deprecated in mlx-lm 0.31.3 | `serve_mlx.sh` uses the `mlx_lm server` subcommand |
| F2 | Gemma 4 on mlx spends 100% of the budget in the reasoning channel, and `content` is never produced | Client sends `enable_thinking: false` for `llama` **and** `mlx` |
| F3 | s1 aborts on a characterless chapter: the model returns `{}` identically on all 9 client + launcher retries | Fixed by the F5 workarounds |
| F4 | `ingest-epub` keeps Gutenberg's "The Project Gutenberg eBook of…" page as chapter 01 | **Open**. Trim it from `novels/<book>/` by hand |
| F5 | mlx-lm ignores per-request sampling and is deterministic per prompt, so validate-and-retry never converges | `serve_mlx.sh --temp` (`LNVOX_LLM_TEMP` 0.2), retry prompt carries the validation error, per-retry temperature escalation |
| F6 | E4B returns schema-valid but empty beats (`{"beats": []}`), and s2's stalled-LLM guard aborts | mlx default moved off E4B (now 12B QAT) |
| F7 | Every Gemma 4 12B MLX conversion is `gemma4_unified`, which released mlx-lm can't load. The failed load **hangs the request** while `/v1/models` stays healthy (a 1.5 h stall at 0% CPU) | Git-pinned mlx-lm @ `8239c72` in `pyproject.toml`. Drop the pin at the next release |
| F8 | Staged `ctx` crashloops with `size mismatch` because DramaBox's Gemma checkpoint is pre-quantized bnb-4bit | `resolve_gemma_root()` downloads unquantized `unsloth/gemma-3-12b-it` whenever `bnb_4bit` is off |
| F9 | `decode` crashloops: `Input type (float) and bias type (c10::Half)` in the vocoder | Decode phase in fp32 on MPS |
| F10 | fp16 Gemma encoder gives all-NaN, producing **36 min of silent audio with exit code 0** | ctx phase in bf16 on MPS |
| F11 | `denoise` crashes on beats ≳25 s: MPS boolean-index shape mismatch in the DiT mask | 22 s / 18 s chunk cap on MPS + `_force_split_oversize()` word-split fallback |
| F12 | Long beats play "whole text fast, then the tail again slow": stale ctx reused after a chunk-policy change | Staged item ids embed the chunk-text hash (`_cNN-<sha8>`) |
| F13 | A cache re-render never reached the m4b, because decode placement skipped existing `05_audio` copies (**also affects CUDA**) | Placement re-copies unless the placed WAV is an exact copy of the cache entry its manifest names (same size, mtime within 2 s); the driver verifies placements before it finishes |
| ops | macOS sleeps mid-render (8.5 h wall clock for ~4 h of compute) | Launcher re-execs under `caffeinate -dis` (`LNVOX_CAFFEINATED`) |

Upstream issues worth filing, per the report: mlx-lm per-request sampling (F5), the mlx-lm hang on a failed lazy load (F7), and the torch-MPS boolean-index bug (F11).

## Known issues and limits

- **Staged s4 only.** The monolithic path (`LNVOX_S4_STAGED=0`, `DramaboxClient`) builds every component in one dtype, and no single dtype works on MPS (F9/F10), so it is refused there: `lnvox s4` without `--staged` exits 2 on MPS, and `s4_retry.sh` and the launcher stop at once instead of retrying.
- **No RE-USE ref denoising on macOS.** `resampy` and `mamba-ssm` have no macOS wheels, so reference clips are used without denoising, and a warning is logged.
- **Unmanaged TTS deps.** Any `uv sync` removes Dramabox's `uv pip` packages, and s4 then fails with `No module named 'torchaudio'`. The launcher reinstalls them. If you run s4 by hand after a sync, re-run `./scripts/setup_dramabox.sh` first, and prefer `.venv/bin/lnvox` over `uv run lnvox` for long jobs so nothing re-syncs mid-run. A sync also replaces an editable lnvox install: after editing lnvox mid-render, `uv pip install --no-deps -e .`.
- **VibeVoice on Mac** was out of scope for the validation. `prepare_vibevoice_env` has an MPS check and `setup_vibevoice.sh` has no platform branch, but it is untested.
- **Beat seams.** Word-split chunks break long sentences mid-way, and the crossfade hides most of it. Splitting over-long beats in s2 instead, at semantic boundaries, is an open design item.
- **Re-validating on a Mac.** `apple.md` at the repo root is the briefing for re-validating this path, kept current (12B QAT, git-pinned mlx-lm, staged-only s4); DESIGN.md §11.3 has the per-component dtype map and measured throughput.
