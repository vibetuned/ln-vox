# Mission briefing: validate the Apple Silicon path (mlx + MPS)

You are a Claude instance running on a Mac, working in a clone of **ln-vox** —
an offline novel→audiobook pipeline (LLM stages direct the performance, a local TTS
renders it, ffmpeg mixes an `.m4b`). The primary dev machine is Linux+CUDA; this Mac
is the **secondary target**. Its path was validated end-to-end once (Aug 2026, M4 Max /
64 GB — `MAC_TEST_REPORT.md`, findings F1–F13: a verified 35-minute Alice `.m4b`,
approved by ear). Your job is to **re-validate** it on this machine and against the
current code, and to document precisely whatever has drifted.

Read these before touching anything:

- `MAC_TEST_REPORT.md` — what broke last time, why, and how it was fixed. Most of the
  watch items below come from it; don't rediscover them the slow way.
- `DESIGN.md` **§11** — the Apple Silicon contract (topology, accepted limitations,
  the per-component MPS dtype map and measured throughput in §11.3).
- `DESIGN.md` §14 — the llama.cpp backend (the pipeline-wide default on every platform).
- `README.md` — "macOS (Apple Silicon) setup" and "Running the pipeline".

## The mission

Prove (or precisely disprove) that DESIGN §11 still works on this machine:

1. **LLM phase on mlx** — `mlx_lm server` serves stages s1/s2/voice-cast/s3 through the
   same OpenAI-compatible `LLMClient` used by vLLM/llama.
2. **TTS phase on MPS** — DramaBox renders s4 with `device="mps"` via the staged path.
3. **End-to-end** — a small public-domain book comes out as a playable, *audible* `.m4b`.
4. A written report of everything that broke, why, and what you fixed.

Success is not "no errors" — it's *documented behavior*: timings, retry rates,
failure modes, and minimal fixes left for the user to review.

## What your clone does NOT have (gitignored)

`novels/`, `epubs/`, `artifacts/`, `cache/`, `voicebank/`, `data/`, `external/`,
`models/`, `scenarios/` are all gitignored. You must obtain:

- **A voicebank** — `voicebank/` (a `manifest.json` + `clips/cv_*.wav`, a few hundred MB).
  **Ask the user to copy it from the Linux workstation** (`rsync`/AirDrop — it drops in at
  the repo root as `voicebank/`). Do NOT try to re-seed it on this Mac: seeding needs the
  ~96 GB Common Voice tarball. No voicebank = no voice casting = no s4.
- **A test book** — download a *short* public-domain EPUB from Project Gutenberg
  (e.g. Alice's Adventures in Wonderland) into `epubs/test/alice.epub`. Short matters:
  the measured render rate is **~4 h of wall clock per hour of audio** (M4 Max), so a full
  novel is a multi-day job. **After `ingest-epub`, keep only the first 1–2 chapter `.txt`
  files** in `novels/test/alice/` before running the pipeline, and pass `--skip-sync`
  (s6 needs the full EPUB to match). `ingest-epub` keeps Gutenberg's "The Project
  Gutenberg eBook of…" boilerplate page as chapter 01 (F4, open) — delete it too unless
  you want it as a robustness probe.
- `external/DramaBox/` — cloned automatically by `./scripts/setup_dramabox.sh` (step below).
  DramaBox auto-downloads ~15 GB of weights from HuggingFace on its first real run, plus
  the **unquantized** Gemma encoder (`unsloth/gemma-3-12b-it`, ~23 GB) on the first s4 —
  the bnb-4bit checkpoint is CUDA-only (F8).

## Phase 0 — machine sanity (do this first, report it)

```bash
sysctl -n machdep.cpu.brand_string   # which Apple chip
sysctl -n hw.memsize                 # unified memory in bytes
uv --version                         # uv is the package manager everywhere
ffmpeg -version                      # s5 needs it: brew install ffmpeg
brew install llama.cpp               # llama-server: the DEFAULT LLM backend, every OS
```

**Memory gate:** bitsandbytes is CUDA-only, so on MPS DramaBox's Gemma prompt-encoder
runs unquantized in **bf16** — budget ≈24 GB for the encoder alone; the full TTS phase
wants **36 GB+ unified memory**. The staged s4 path loads one model per phase, which is
what makes that fit. If this Mac has less, still run Phases 1–2 (LLM side), attempt s4
anyway, and report the OOM precisely — that's a valid finding, not a failure of the
mission.

Then sync and run the pure-logic tests as a cheap import sanity check (they run with
plain python, no pytest needed — skip `test_m4b_images.py` and
`test_voicebank_quality.py`, which need pytest):

```bash
uv sync --extra mlx --extra voice --extra tts
for t in tests/test_*.py; do uv run python "$t" || echo "FAILED: $t"; done
```

The `mlx` extra installs **mlx-lm from git**, pinned in `pyproject.toml`'s
`[tool.uv.sources]` (rev `8239c72…`): every `mlx-community` Gemma 4 **12B** conversion
declares `model_type: gemma4_unified`, which no PyPI mlx-lm release (≤ 0.31.3) can load —
the fix, ml-explore/mlx-lm#1349, is merged but unreleased, and git main still
self-reports 0.31.3 (F7). Keep the pin in `[tool.uv.sources]`: a
`uv pip install git+…` would be undone by the launcher's next `uv sync`. If a PyPI
release > 0.31.3 now exists, test it and report whether the pin can go.

## Phase 1 — mlx LLM server, standalone

Boot the server alone before involving the pipeline:

```bash
./scripts/serve_mlx.sh          # mlx-community/gemma-4-12B-it-qat-4bit (~7 GB) on :8000
curl -s http://127.0.0.1:8000/v1/models
# then one manual chat completion asking for a small JSON object, sent with
# "chat_template_kwargs": {"enable_thinking": false} as LLMClient does — check that
# `choices[0].message.content` is non-empty JSON, not empty/reasoning-only.
```

`serve_mlx.sh` runs the `mlx_lm server` subcommand (`python -m mlx_lm server …`; the
old `python -m mlx_lm.server` module form is deprecated — F1) with a server-wide
`--temp` (`LNVOX_LLM_TEMP`, default 0.2). It reads env vars only.

Watch items (each has bitten this project before):

- **A failed model load hangs the request (F7).** When mlx-lm can't load the model it
  logs the error but never answers the pending HTTP request, while `/v1/models` keeps
  returning 200 — the launcher thinks the server is healthy and s1 sits at 0% CPU for
  as long as the client's token-scaled timeout (up to ~1.5 h). Detection cue: server
  and stage both idle, HF cache no longer growing. Check the serve log first.
- **Thinking-channel burn (F2).** Gemma 4's chat templates enable "thinking" by default
  and burn the whole budget in the reasoning channel (empty `content`,
  `finish_reason: "length"`). `LLMClient.structured` sends `enable_thinking: false` for
  **both** the llama and mlx backends, keyed on `LNVOX_LLM_BACKEND` — so a stage run
  ad hoc without `export LNVOX_LLM_BACKEND=mlx` still gets it (unset behaves as llama),
  but a manual curl doesn't. Everything landing in `reasoning` = this, again.
- **Per-request sampling is ignored (F5).** mlx-lm 0.31.3's batched path ignores
  per-request sampling params and is deterministic per prompt, so an identical retry
  replays an identical (invalid) completion. The mitigations are the server-wide
  `--temp` and the client appending the previous validation error to each retry
  prompt. If a stage aborts on the same parse failure N times in a row, check both.
- **Model choice.** 12B QAT is the default for a reason: E4B
  (`mlx-community/gemma-4-E4B-it-4bit`, 16 GB Macs) returned degenerate `{}` and
  schema-valid-but-empty beats on s1/s2 (F3/F6); 31B QAT
  (`mlx-community/gemma-4-31B-it-qat-4bit`, 32 GB+) loads on any mlx-lm but spent
  33 min on one chapter of s2. Note any substitution you make.

## Phase 2 — LLM pipeline stages on mlx

The launcher manages the server lifecycle, venv swaps, and stage ordering:

```bash
./scripts/run_pipeline.sh test/alice \
    --llm-backend mlx \
    --narrator-clip <cv_id from `uv run lnvox voice list`> \
    --book-title "Alice — MLX smoke test" \
    --skip-sync
```

Critical context:

- **The default backend is `llama` on every platform** (changed 2026-07-25). You are
  testing mlx **only** because of the explicit `--llm-backend mlx`. Don't drop the flag.
  (With it, the launcher defaults the model to `mlx-community/gemma-4-12B-it-qat-4bit`;
  `--llm-model` or an exported `LNVOX_LLM_MODEL` picks another.)
- If you run stages **ad-hoc** (`uv run lnvox s1 …`) instead of through the launcher,
  `export LNVOX_LLM_BACKEND=mlx` first. The launcher exports it for you.
- **Expected degradations on mlx** (DESIGN §11.2 — accepted, not bugs): no `guided_json`
  (so a higher first-attempt JSON parse-fail rate — the client validates and retries;
  *count the retries and report the rate*), no `repetition_penalty`, no prefix caching
  (slower multi-call stages).
- **Calibration** from the first Mac run (12B QAT, boilerplate page + Alice ch. I–II,
  cached weights): s1 ~70 s, s2 ~11 min, voice cast ~1.5 min, s3 ~2.5 min — ~15 min for
  the whole LLM phase with no stage failures. Minutes-long single calls mean something
  is wrong (see F2/F7 above).

If mlx-lm won't serve at all, run the same command with `--llm-backend llama` (same
client, GGUF via brew's llama-server) to keep the rest of the mission moving — but
report the mlx failure; mlx is the mission.

## Phase 3 — DramaBox TTS on MPS

```bash
./scripts/setup_dramabox.sh   # Darwin branch: filters torch*/bitsandbytes out of
                              # DramaBox's reqs, installs MPS-capable PyPI torch
                              # (>=2.10,<2.12), verifies torch.backends.mps
```

The launcher (Phase 2 command) continues into s4 automatically. What to know:

- **Device is auto-detected** — `lnvox s4` picks `mps` via
  `torch.backends.mps.is_available()` (`_default_tts_device` in `src/lnvox/cli.py`), and
  the staged driver hands it to every phase subprocess. No flag needed.
- **Staged s4 is the launcher default** (`LNVOX_S4_STAGED=1`) and the **only** s4 path
  that works on MPS. It assigns a dtype per phase (`src/lnvox/tts/staged.py`):

  | phase | MPS dtype | why |
  |---|---|---|
  | `refs` (audio VAE, RE-USE) | fp16 | works as-is |
  | `ctx` (Gemma encoder) | **bf16**, unquantized checkpoint | fp16 Gemma encodes to 100% NaN → **silent audio with exit 0** (F10); bnb-4bit is CUDA-only (F8) |
  | `denoise` (DiT) | fp16, chunks capped at **22 s** (target 18 s) | a torch-MPS boolean-indexing bug breaks the DiT mask on beats ≳25 s (F11); a word-split fallback handles single over-long sentences |
  | `decode` (vocoder) | **fp32** | fp16 hits `Input type (float) and bias type (c10::Half)` (F9) |

  bnb-4bit and `torch.compile` are off on MPS. **`LNVOX_S4_STAGED=0` doesn't work
  here**: the monolithic `DramaboxClient`/`TTSServer` builds every component in one
  dtype, and no single dtype works on MPS, so `lnvox s4` without `--staged` refuses MPS
  (exit 2 — `s4_retry.sh` and the launcher stop at once) instead of rendering silence.
  That's also why the Studio's TTS lab uses `lnvox.tts.lab_engine.StagedLabEngine` on
  MPS.
- **Do NOT patch DramaBox's source.** `torch.cuda.empty_cache()` etc. are called
  unconditionally inside it; §11.3 explicitly accepts that as failure surface. If a call
  site kills the run, capture the full traceback for the report — the fix belongs in
  our wrapper / planner (`src/lnvox/tts/dramabox_client.py`, `src/lnvox/tts/staged.py`),
  never in `external/DramaBox/`.
- **RE-USE ref denoising is unavailable on macOS** (`resampy`/`mamba-ssm` have no
  wheels): refs are used un-denoised with a warning. Expected, not a bug.
- **Throughput expectation** (M4 Max, final config): ctx ~2 s/chunk, denoise ~69 s per
  ≤22 s chunk (~3.6× real time), decode ~3 s/chunk — **~4 h of render per hour of
  audio**. Record your actual seconds-of-audio-per-minute-of-wall-clock. The launcher
  re-execs itself under `caffeinate -dis` so the machine doesn't sleep mid-render (it did
  once: 4 h of compute took 8.5 h); an `lnvox s4` run by hand needs its own
  `caffeinate -dis …`.
- **Staged intermediates:** item ids embed a hash of the chunk text (`_cNN-<sha8>`), so a
  chunk-policy change invalidates stale ctx automatically (F12) — never hand-purge
  `_staged/` files selectively. When checking whether a fix changed the audio, hash the
  m4b **and** compare `05_audio` copies against their `cache/tts` sources (F13).
- **Venv gotcha (cost a debugging session before):** DramaBox's deps are installed
  *unmanaged* via `uv pip` — any later `uv sync` silently removes them and s4 crashloops
  on `No module named 'torchaudio'`. The launcher's `prepare_tts_env` restores them; if
  you run s4 by hand after any sync, re-run `./scripts/setup_dramabox.sh` first, and
  prefer `.venv/bin/lnvox` over `uv run lnvox` for long TTS jobs so nothing re-syncs
  mid-run. `uv sync` also replaces an editable lnvox install: after hand-editing lnvox
  mid-TTS, `uv pip install --no-deps -e .` (add `--offline` if PyPI DNS flakes).
- Keep `transformers>=4.52` in the env (the Gemma encoder needs it). VibeVoice (the
  second TTS engine) pins 4.51.3 and can't share a venv with DramaBox — **VibeVoice is
  out of scope for this test**; don't set it up.
- LLM and TTS phases must not run at once — both want most of the memory. The launcher
  already stops the LLM server before s4.

## Phase 4 — mix and verify

s5 (ffmpeg, CPU-only) runs automatically after s4. The launcher prints the final path
(`artifacts/test/alice/06_final/<title>.m4b`, with any `/` or `\` in the title replaced
by `_`). Verify:

- It exists, plays (use `afplay` or `ffprobe` for duration), and has chapter markers
  (`ffprobe -show_chapters`).
- **It is not silent.** F10 produced a valid, chaptered `.m4b` of digital zero with exit
  code 0 — the only signs were the size (1.1 MB for "36 minutes") and loudness:

  ```bash
  ls -la artifacts/test/alice/06_final/
  ffmpeg -i "artifacts/test/alice/06_final/<title>.m4b" -af volumedetect -f null - 2>&1 \
      | grep -E 'mean_volume|max_volume'      # silence reads −91 dB
  ```

  A healthy render is ~45–50 MB per hour of audio at the default 96 kbps (the validated
  Alice build: 25.7 MB for 35 min, mean −19…−22 dB).
- Actually **listen to ~30 seconds** if you can, or ask the user to — "renders without
  error" and "sounds like acted speech" are different claims. Flag it for the user's ears
  either way.

## Report + rules of engagement

- **Do not commit, stage, push or open a PR** unless the user explicitly asks — this is
  the user's standing instruction for every repo. Leave every change as an uncommitted,
  unstaged edit in the working tree; the user reviews through the IDE's working-tree
  diff. Creating a branch is fine if they ask for one.
- Append a new dated section to `MAC_TEST_REPORT.md` (don't rewrite the first run's):
  machine specs, package versions (`mlx-lm` — note git rev vs release — `torch`,
  `transformers`), per-stage wall-clock, JSON parse-retry counts, every failure with its
  traceback, every fix with its rationale. Stale doc call-outs (DESIGN/README claims that
  turned out false on Mac) are first-class findings.
- Fix-forward is welcome for *small, Mac-gated* issues (script args, env detection,
  dtype/chunk knobs in the staged planner). Anything architectural (new fallback layers,
  guided-decoding shims, DramaBox forks): DESIGN §11.5 explicitly defers those — write it
  up as a finding instead. This project's convention is design-doc-first for new
  components.
- If a `scenarios/` directory exists on this machine, its contents are the user's
  troupe's **protected IP**: never quote, excerpt, or paraphrase script text into code,
  prompts, tests, logs, or the report. Invented examples only. (Same spirit for any
  copyrighted novel text: quote sparingly, only what a bug report strictly needs.)
- When blocked on something only the user can provide (the voicebank transfer, a
  bigger-memory decision, listening verdicts), state the blocker clearly in the report
  and move to the next phase you *can* run — don't stall the whole mission.
