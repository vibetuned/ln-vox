# Mission briefing: validate the Apple Silicon path (mlx + MPS)

You are a Claude instance running on a Mac, working in a fresh clone of **ln-vox** —
an offline novel→audiobook pipeline (LLM stages direct the performance, a local TTS
renders it, ffmpeg mixes an `.m4b`). The primary dev machine is Linux+CUDA; this Mac
is the **secondary target** and its path has **never been smoke-tested end-to-end**.
Your job is to change that.

Read these before touching anything:

- `DESIGN.md` **§11** — the Apple Silicon contract (topology, accepted limitations). This is your spec.
- `DESIGN.md` §14 — the llama.cpp backend (your fallback if mlx-lm won't start).
- `README.md` — "One-time setup" and "Running the pipeline".

## The mission

Prove (or precisely disprove) that DESIGN §11 works on this machine:

1. **LLM phase on mlx** — `mlx_lm.server` serves stages s1/s2/voice-cast/s3 through the
   same OpenAI-compatible `LLMClient` used by vLLM/llama.
2. **TTS phase on MPS** — DramaBox renders s4 with `device="mps"`.
3. **End-to-end** — a small public-domain book comes out as a playable `.m4b`.
4. A written report of everything that broke, why, and what you fixed.

Success is not "no errors" — it's *documented behavior*: timings, retry rates,
failure modes, and minimal fixes on a branch.

## What your clone does NOT have (gitignored)

`novels/`, `epubs/`, `artifacts/`, `cache/`, `voicebank/`, `data/`, `external/`,
`models/`, `scenarios/` are all gitignored. You must obtain:

- **A voicebank** — `voicebank/` (a `manifest.json` + `clips/cv_*.wav`, a few hundred MB).
  **Ask the user to copy it from the Linux workstation** (`rsync`/AirDrop — it drops in at
  the repo root as `voicebank/`). Do NOT try to re-seed it on this Mac: seeding needs the
  ~96 GB Common Voice tarball. No voicebank = no voice casting = no s4.
- **A test book** — download a *short* public-domain EPUB from Project Gutenberg
  (e.g. Alice's Adventures in Wonderland) into `epubs/test/alice.epub`. Short matters:
  DramaBox on MPS is expected to render at 5–10× real-time (DESIGN §11.3), so a full novel
  is a multi-day job. **After `ingest-epub`, keep only the first 1–2 chapter `.txt` files**
  in `novels/test/alice/` before running the pipeline, and pass `--skip-sync` (s6 needs the
  full EPUB to match).
- `external/DramaBox/` — cloned automatically by `./scripts/setup_dramabox.sh` (step below).
  DramaBox also auto-downloads ~15 GB of weights from HuggingFace on its first real run.

## Phase 0 — machine sanity (do this first, report it)

```bash
sysctl -n machdep.cpu.brand_string   # which Apple chip
sysctl -n hw.memsize                 # unified memory in bytes
uv --version                         # uv is the package manager everywhere
ffmpeg -version                      # s5 needs it: brew install ffmpeg
brew install llama.cpp               # fallback LLM backend (native binary)
```

**Memory gate:** with `bnb_4bit=False` (bitsandbytes is CUDA-only), DramaBox's Gemma
prompt-encoder loads in fp16 — budget ≈24 GB for the encoder alone; DESIGN §11.3 says
the full TTS phase wants **36 GB+ unified memory**. If this Mac has less, still run
Phases 1–2 (LLM side), attempt s4 anyway, and report the OOM precisely — that's a
valid finding, not a failure of the mission.

Then run the pure-logic tests as a cheap import sanity check (they run with plain
python, no pytest needed):

```bash
uv sync --extra mlx --extra voice --extra tts
for t in tests/test_*.py; do uv run python "$t" || echo "FAILED: $t"; done
```

## Phase 1 — mlx LLM server, standalone

Boot the server alone before involving the pipeline:

```bash
./scripts/serve_mlx.sh          # defaults to mlx-community/gemma-4-E4B-it-4bit on :8000
curl -s http://127.0.0.1:8000/v1/models
# then one manual chat completion asking for a small JSON object — check that
# `choices[0].message.content` is non-empty JSON, not empty/reasoning-only.
```

Watch items (each has bitten this project before, or is an anticipated risk):

- **Checkpoint availability.** `mlx-community/gemma-4-E4B-it-4bit` is the default pick
  (`scripts/serve_mlx.sh:20`); if that repo doesn't exist on HF, pick the closest
  MLX-quantized Gemma 4 and note the substitution. 32 GB+ Macs can try
  `mlx-community/gemma-4-31B-it-4bit`.
- **mlx-lm CLI drift.** The script runs `python -m mlx_lm.server`. If mlx-lm has renamed
  its entry points since `mlx-lm>=0.20`, fix the script and note the version.
- **Empty `content` = Gemma thinking-channel burn.** Known failure mode on this project:
  Gemma 4 chat templates that enable "thinking" by default burn the whole token budget in
  the reasoning channel and return empty content. The fix
  (`chat_template_kwargs: {"enable_thinking": false}`) is currently **gated to the llama
  backend only** — see `src/lnvox/llm/client.py:135`. If you see empty content /
  parse failures with everything in reasoning tokens on mlx, widening that gate (or
  sending the kwarg unconditionally — the code comment claims mlx ignores it safely)
  is the first fix to try. This is the single most likely mlx-specific bug.

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

- **The default backend is `llama` on every platform** (changed 2026-07-25) — the old
  Darwin→mlx auto-default is gone. You are testing mlx **only** because of the explicit
  `--llm-backend mlx`. Don't drop the flag.
- If you run stages **ad-hoc** (`uv run lnvox s1 …`) instead of through the launcher,
  `export LNVOX_LLM_BACKEND=mlx` first. The launcher exports it for you; a bare stage
  call without it behaves as llama-backend client-side.
- **Expected degradations on mlx** (DESIGN §11.2 — accepted, not bugs): no `guided_json`
  (so a higher first-attempt JSON parse-fail rate — the client validates and retries;
  *count the retries and report the rate*), no `repetition_penalty`, no prefix caching
  (slower multi-call stages).
- Calibration from the Linux box: a healthy s1 merge call returns in ~2 s on
  llama/12B-QAT. Minutes-long single calls on a small model mean something is wrong
  (see the thinking-channel item above).

If mlx-lm won't serve at all, the designated recovery path is
`--llm-backend llama` (same client, GGUF via brew's llama-server) — validating that on
macOS is a worthwhile consolation prize, but mlx is the mission.

## Phase 3 — DramaBox TTS on MPS

```bash
./scripts/setup_dramabox.sh   # Darwin branch: filters torch*/bitsandbytes out of
                              # DramaBox's reqs, installs MPS-capable PyPI torch
                              # (>=2.10,<2.12), verifies torch.backends.mps
```

The launcher (Phase 2 command) continues into s4 automatically. What to know:

- **Device is auto-detected** — `lnvox s4` picks `mps` via
  `torch.backends.mps.is_available()` (`src/lnvox/cli.py:658`). No flag needed.
- **MPS knob set** (DESIGN §11.3, applied inside `DramaboxClient.__init__`):
  `dtype=fp16`, `bnb_4bit=False`, `compile_model=False`. The CUDA path is untouched.
- **Staged s4 is the launcher default** (`LNVOX_S4_STAGED=1`): four checkpointed
  subprocess phases, each loading ONE model — probably *friendlier* to unified memory,
  but never tested on MPS. If the staged driver misbehaves, retry with
  `LNVOX_S4_STAGED=0` (monolithic + `s4_retry.sh` auto-resume) and report which of the
  two works. The content-hash cache makes every restart a cheap resume either way.
- **Do NOT patch DramaBox's source.** `torch.cuda.empty_cache()` etc. are called
  unconditionally inside it; §11.3 explicitly accepts that as failure surface. If a call
  site kills the run, capture the full traceback for the report — the fix belongs in
  our wrapper (`src/lnvox/tts/dramabox_client.py`), never in `external/DramaBox/`.
- **Throughput expectation:** 5–10× real-time. A 20-minute chapter ≈ 2–3 hours. This is
  why your test book is 1–2 chapters. Record actual seconds-of-audio-per-minute-of-wall-clock.
- **Venv gotcha (cost a debugging session before):** DramaBox's deps are installed
  *unmanaged* via `uv pip` — any later `uv sync` silently removes them and s4 crashloops
  on `No module named 'torchaudio'`. The launcher's `prepare_tts_env` restores them; if
  you run s4 by hand after any sync, re-run `./scripts/setup_dramabox.sh` first, and
  prefer `.venv/bin/lnvox` over `uv run lnvox` for long TTS jobs so nothing re-syncs
  mid-run.
- Keep `transformers>=4.52` in the env (the Gemma encoder needs it). VibeVoice (the
  second TTS engine) pins 4.51.3 and can't share a venv with DramaBox — **VibeVoice is
  out of scope for this test**; don't set it up.
- LLM and TTS phases must not run at once — both want most of the memory. The launcher
  already stops the LLM server before s4.

## Phase 4 — mix and verify

s5 (ffmpeg, CPU-only) runs automatically after s4. Verify:

- `artifacts/test/alice/06_final/*.m4b` exists, plays (use `afplay` or `ffprobe` for
  duration), and has chapter markers (`ffprobe -show_chapters`).
- Actually **listen to ~30 seconds** if you can, or ask the user to — "renders without
  error" and "sounds like acted speech" are different claims; MPS fp16 could plausibly
  produce degraded audio that no exit code catches. Flag it for the user's ears either way.

## Report + rules of engagement

- Work on a branch (e.g. `mac-mlx-validation`), small commits, never force-push. Open a
  PR rather than committing to `main`.
- Keep a running `MAC_TEST_REPORT.md` at the repo root: machine specs, package versions
  (`mlx-lm`, `torch`, `transformers`), per-stage wall-clock, JSON parse-retry counts,
  every failure with its traceback, every fix with its rationale. Stale doc call-outs
  (DESIGN/README claims that turned out false on Mac) are first-class findings.
- Fix-forward is welcome for *small, Mac-gated* issues (script args, env detection, the
  thinking-channel gate). Anything architectural (new fallback layers, guided-decoding
  shims, DramaBox forks): DESIGN §11.5 explicitly defers those — write it up as a
  finding instead. This project's convention is design-doc-first for new components.
- If a `scenarios/` directory exists on this machine, its contents are the user's
  troupe's **protected IP**: never quote, excerpt, or paraphrase script text into code,
  prompts, tests, logs, or the report. Invented examples only. (Same spirit for any
  copyrighted novel text: quote sparingly, only what a bug report strictly needs.)
- When blocked on something only the user can provide (the voicebank transfer, a
  bigger-memory decision, listening verdicts), state the blocker clearly in the report
  and move to the next phase you *can* run — don't stall the whole mission.
