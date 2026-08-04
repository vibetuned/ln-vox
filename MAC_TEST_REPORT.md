# Mac (Apple Silicon) validation report — mlx + MPS path

Mission: validate DESIGN.md §11 end-to-end on a Mac — LLM stages on
`mlx_lm.server`, DramaBox s4 on MPS, and a playable `.m4b` out the far end.
Branch: `mac-mlx-validation` (working tree only — no commits/PR yet, per
user request; manual verification first).

**Status: COMPLETE — the DESIGN §11 path WORKS on this machine.** A
playable, chapter-marked, loudness-verified 35-minute `.m4b` of Alice
ch. I–II came out the far end (LLM stages on mlx_lm.server, DramaBox s4 on
MPS, ffmpeg mix). It took 11 pipeline attempts and 11 findings to get
there; every fix is small and Mac-gated, on this branch, unreviewed.
**Listening verdict (2026-08-03): APPROVED** — the user judges this render
*better to the ear* than the CUDA renders, at a real-time cost. See
"Listening verdict & CUDA backport candidates" at the end.

## Machine

| item | value |
|---|---|
| Chip | Apple M4 Max |
| Unified memory | 64 GB (`hw.memsize` = 68719476736) — clears the §11.3 36 GB gate |
| OS | macOS 26.6 (build 25G72), Darwin 25.6.0 |
| Python | 3.13.13 (uv-managed, per `.python-version`) |
| uv | 0.11.16 |
| ffmpeg | 8.1.2 (brew, installed during this run) |
| llama.cpp | brew build 10210 (installed as fallback; not needed so far) |

## Package versions (LLM phase venv)

| package | version |
|---|---|
| mlx-lm | 0.31.3 |
| mlx | 0.31.2 |
| transformers | 5.9.0 |
| pydantic | 2.13.4 |
| openai | 2.38.0 |

## Phase 0 — sanity

- `uv sync --extra mlx --extra voice --extra tts`: clean. Confirmed the
  documented gotcha live: the sync **removed** `torch`/`torchaudio` left by a
  prior DramaBox install (they're unmanaged `uv pip` overlays). The launcher's
  `prepare_tts_env` is what restores them before s4.
- All 7 pure-logic tests pass (`tests/test_*.py` run with plain python).
- Voicebank already present at `voicebank/` (402 clips: 402 common_voice).
- Narrator clip for the smoke test: `cv_002cb63172eb` (male/adult).

## Test book

- `epubs/test/alice.epub` — Alice's Adventures in Wonderland, Gutenberg
  EPUB3. `ingest-epub` → 13 clean chapters + cover image. Trimmed
  `novels/test/alice/` to the first 3 files: Gutenberg boilerplate page
  (334 chars — see finding F4), Chapter I (11.9k chars), Chapter II
  (11.3k chars). `--skip-sync` passed since the book is truncated.

## Phase 1 — mlx_lm.server standalone

- Checkpoint: script default `mlx-community/gemma-4-E4B-it-4bit` **works**.
  HF renamed the repo to lowercase `gemma-4-e4b-it-4bit`; hub redirects
  resolve it transparently, no script change needed. (It's tagged
  `image-text-to-text`/mlx-vlm, but mlx-lm 0.31.3 loads the text stack fine.)
  Weights fetch: ~30 s (xet-backed).
- Server boots via `scripts/serve_mlx.sh`, `/v1/models` responds, OpenAI
  contract holds.

### Finding F1 — mlx-lm CLI drift (fixed)

`python -m mlx_lm.server` is **deprecated** in mlx-lm 0.31.3 (warns; the
consolidated CLI is `mlx_lm server`). `scripts/serve_mlx.sh` updated to the
new form. Still boots either way today — this is future-proofing before the
alias is removed.

### Finding F2 — Gemma thinking-channel burn on mlx (the predicted bug — confirmed, fixed)

Exactly the anticipated failure mode, reproduced on the first manual
completion: with the stock chat template, **100% of the token budget goes to
the `reasoning` channel**; `content` never appears; `finish_reason: "length"`.

- Repro (manual curl, max_tokens=200): 200 completion tokens, all in
  `message.reasoning`, no `message.content`.
- With `chat_template_kwargs: {"enable_thinking": false}`: clean JSON in
  `content`, 24 tokens, `finish_reason: "stop"`, ~0.5 s.

Fix: widened the backend gate at `src/lnvox/llm/client.py` (was
`== "llama"`, now `in ("llama", "mlx")`) so the kwarg is sent on mlx too.
vLLM path untouched, per §11's "CUDA byte-identical" rule. mlx-lm 0.31.3
demonstrably honors the kwarg per-request.

## Phase 2 — LLM pipeline stages on mlx

Launcher command:

```bash
./scripts/run_pipeline.sh test/alice --llm-backend mlx \
    --narrator-clip cv_002cb63172eb --book-title "Alice - MLX smoke test" --skip-sync
```

- Launcher lifecycle works on Darwin: mlx venv prep, background
  `serve_mlx.sh` start, readiness poll (~3 s with cached weights), stages
  in order, server stop before TTS.

### Finding F5 — mlx_lm.server sampling is deterministic per request: the retry contract is broken (worked around; upstream bug)

**The most consequential mlx finding.** `mlx_lm.server` 0.31.3:

1. **Ignores per-request sampling params in its batched decode path.** Two
   identical requests with `"temperature": 1.5` in the body return
   byte-identical output. The server-side default applies instead — and that
   default is `--temp 0.0`, i.e. **greedy**.
2. **Even with a server-side `--temp`, the sampler is seeded
   deterministically per request.** With `--temp 1.2`, three identical
   requests return byte-identical (but different-from-greedy) output. So the
   temperature is applied, but an identical prompt maps to exactly one
   completion at any temperature.

Consequence: `LLMClient.structured()`'s validate-and-retry loop — the §11.2
mechanism that is supposed to absorb the missing `guided_json` — **can never
converge** on this server: every retry replays the same invalid completion.
Both pipeline aborts below (F3, and the s2 abort in run 2) were this bug
wearing different hats: 9/9 identical `{}` in s1; 6/6 identical
invalid-JSON in s2 (unescaped quote at the same byte, `completion_tokens=895`
every time).

Fixes applied:

- `scripts/serve_mlx.sh`: serve with `--temp` (new `LNVOX_LLM_TEMP`, default
  0.2 to match `LLMConfig`) instead of inheriting greedy. Necessary but not
  sufficient (see 2 above).
- `src/lnvox/llm/client.py` (`structured()` retry loop): retries no longer
  replay a byte-identical prompt — the previous attempt's validation error is
  appended to the user message ("retry N: … failed validation with: …
  escape all double quotes…"). This both breaks the per-request determinism
  and gives the model a corrective signal; it helps the llama backend too,
  and vLLM's guided path essentially never reaches it. Also: temperature
  escalation per retry (`min(1.0, temp + 0.4*attempt)`) for backends that
  DO honor per-request temperature (llama).

Upstream: worth filing against mlx-lm (per-request sampling params dropped in
the batched path + per-request RNG determinism). Until then the client-side
workaround is load-bearing.

### Finding F3 — deterministic degenerate `{}` on a characterless chapter (fixed via F5 workarounds)

First full pipeline run aborted in s1: the Gutenberg boilerplate page (no
characters in the text) made the model return literally `{}`
(`finish_reason=stop`, 2 completion tokens), failing `CharacterList`
validation. All 3 client attempts **and** all 3 launcher stage-retries got
the identical `{}` — retries at the same temperature just re-sample the same
degenerate mode.

Root cause chain:

1. `mlx_lm.server` 0.31.3 ignores `guided_json` **and**
   `response_format` entirely (verified by reading the installed
   `mlx_lm/server.py` — no handling for either). This is the §11.2.1
   accepted degradation: nothing forces the `characters` key.
2. On a chapter with no extractable characters, bare `{}` is the model's
   greedy completion — and per F5 the server was decoding greedily AND
   deterministically, so all 9 attempts replayed it byte-for-byte. Real
   chapters were unaffected (Chapter I returned 438 tokens of schema-valid
   JSON on the same server minutes later).

Fixed by the F5 workarounds (server `--temp` + retry-prompt feedback +
retry temperature escalation). §11.5 explicitly rules out a guided-decoding
shim; everything stays inside the existing validate-and-retry contract.

### Finding F6 — E4B quality floor: schema-valid but *empty* beats; mlx default switched to 12B QAT (fixed by model choice)

With the F5 workarounds in place, run 3 (E4B) got past the JSON-parse
failures — the retry-feedback prompt recovered chapter 02's s2 call on its
first corrective attempt. It then died deeper in s2: scene `02_s11`
(124 chars of source) returned `{"beats": []}` — **schema-valid**, so the
validate-and-retry loop is satisfied and the retry-feedback never fires; the
stage-level "0 beats = stalled LLM" guard aborts instead, and per F5 every
stage retry replays the identical empty response. A failure class no retry
machinery can absorb: the model is simply too weak for the task on this
input shape.

Fix (suggested by the user, matches the pipeline-wide 2026-07-25 llama
default): move the mlx default off E4B to a QAT variant. First attempt was
`mlx-community/gemma-4-12B-it-qat-4bit` — which turned out to be unloadable
(F7). Final default: **`mlx-community/gemma-4-31B-it-qat-4bit`**. E4B remains
documented in the script comments as the 16 GB-Mac dev pick, with a warning.
E4B artifacts were wiped and the pipeline re-run clean for untainted
provenance.

### Finding F7 — ALL MLX Gemma 4 12B conversions are unloadable by mlx-lm; failed loads hang the request (model default → 31B QAT)

Attempting the 12B QAT default produced a **silent 1.5 h hang**:

- Every `mlx-community` Gemma 4 **12B** repo (`-it-qat-4bit`, `-it-4bit`,
  `-it-8bit`, `-it-bf16`, `-it-qat-OptiQ-4bit`, `-12B-4bit`,
  `-it-assistant-*`) ships `model_type: gemma4_unified` (or
  `gemma4_unified_assistant`) — **not supported by any released mlx-lm**
  (0.31.3 is current on PyPI: `ValueError: Model type gemma4_unified not
  supported`). Only the older `gemma4`-type conversions load: the E4B/E2B
  family and the **31B** family (`gemma-4-31B-it-4bit`,
  `gemma-4-31B-it-qat-4bit`).
- Upstream bug #2: when the lazy model load fails, `mlx_lm.server` logs the
  ValueError but **never responds to the pending HTTP request**. `/v1/models`
  keeps returning 200 (so the launcher thinks the server is healthy) while
  the stage call blocks. Combined with `LLMClient`'s token-scaled timeout
  (~104 min at 24576 max_tokens), s1 sat at 0% CPU for ~1.5 h with no error.
  Detection cue for humans: server + stage processes both at 0% CPU, HF
  cache no longer growing.

Model defaults now: `mlx-community/gemma-4-31B-it-qat-4bit` (~17 GB 4-bit,
32 GB+ Macs — fine on this 64 GB M4 Max; §11.2 already listed 31B as the
"heavier prod pick").

**Update (user-supplied lead):** the `gemma4_unified` fix is upstream PR
[ml-explore/mlx-lm#1349](https://github.com/ml-explore/mlx-lm/pull/1349) —
**merged 2026-06-05** (commit `8239c72`) but **not yet in any PyPI release**
(0.31.3 predates it, and git main still self-reports 0.31.3, so the version
string cannot distinguish fixed from broken). Verified in an isolated venv:
`pip install git+https://github.com/ml-explore/mlx-lm.git` loads
`gemma-4-12B-it-qat-4bit` and generates.

**Resolution (final):** 31B proved far too slow on this M4 Max — s2 spent
**33 minutes on a single chapter** (~11 min even on the 334-char boilerplate
page); a real book would take days in the LLM phase alone. Per the user's
call, the default is now **`mlx-community/gemma-4-12B-it-qat-4bit`** served
by a **git-pinned mlx-lm** (`[tool.uv.sources] mlx-lm = { git = …,
rev = "8239c72…" }` in `pyproject.toml`) — a plain `uv pip install git+…`
would NOT survive the launcher's `uv sync` (same unmanaged-overlay trap as
the DramaBox deps). Drop the pin at the first mlx-lm release > 0.31.3.
31B and E4B stay documented in `serve_mlx.sh` comments as alternates with
their trade-offs.

### Finding F4 — ingest-epub front-matter filter misses the Gutenberg boilerplate page (open, not fixed)

`ingest-epub` claims to drop front matter, but Gutenberg's "The Project
Gutenberg eBook of…" metadata page came through as chapter
`01-the-project-gutenberg-ebook-of…txt`. It's what triggered F3, and it will
be narrated into the audiobook. Left in place for this run (it became a
useful robustness probe). A filter tweak is content-heuristic work — flagged
rather than fixed (design-first convention).

## Phase 3 — DramaBox s4 on MPS

### Phase 2 final timings (12B QAT, run 6)

Whole LLM phase (3 chapters: boilerplate + Alice ch. I–II): **~15 min**,
zero stage-level failures, zero visible parse-retry aborts.

| stage | wall-clock |
|---|---|
| s1 cast extraction | ~70 s (3 chapters; incl. model load) |
| s2 scene segmentation | ~11 min (10 scenes, 154 beats) |
| Stage V voice cast | ~1.5 min |
| s3 director | ~2.5 min |

For comparison: 31B QAT spent **33 min on one chapter** in s2 (run 5,
killed); E4B was fast but structurally unreliable (F3/F6).

### Finding F8 — DramaBox's Gemma encoder checkpoint is bnb-4bit-only; fp16 path needs a different checkpoint (fixed in wrapper)

First-ever s4 on MPS: the staged driver's `refs` phase (audio VAE +
RE-USE denoiser) **worked on MPS out of the box**. The `ctx` phase
crashlooped 10× and aborted: per-layer `size mismatch … copying a param
with shape torch.Size([7864320, 1]) … current model is
torch.Size([4096, 3840])`.

Root cause: §11.3's "with `bnb_4bit=False` the encoder loads in fp16"
assumed the checkpoint on disk is fp16. It isn't — DramaBox's downloader
only fetches `unsloth/gemma-3-12b-it-bnb-4bit`, whose weights are stored
**pre-quantized** (packed `[N,1]` uint8 blobs) and only bitsandbytes
(CUDA-only) can expand them. `bnb_4bit=False` changes the load path, not
the artifact. Upstream even documents this: `--no-bnb-4bit` "use only if
--gemma-root points at an unquantized Gemma checkpoint".

Fix (wrapper-only, per §11.3's no-DramaBox-patches rule):
`resolve_gemma_root()` in `src/lnvox/tts/dramabox_client.py` — when the
effective `bnb_4bit` is False, `snapshot_download("unsloth/gemma-3-12b-it")`
(ungated bf16 mirror, ~24 GB one-time) and pass that as `gemma_root`.
Wired into both `DramaboxClient.__init__` and the staged driver's ctx
phase (`src/lnvox/tts/staged.py`). CUDA path untouched (still gets
`paths["gemma_root"]`).

### Finding F9 — MPS fp16 dtype clash in the vocoder; decode phase now fp32 on MPS (fixed)

With the encoder fixed, s4 ran refs → ctx (~2 s/item × 137) → dit
(~2.1 h for ~36 min of audio) and then crashlooped in `decode`:
`RuntimeError: Input type (float) and bias type (c10::Half)` inside
DramaBox's `AudioDecoder` conv1d — an MPS op silently upcasts an
intermediate to fp32, which then meets fp16 weights. Deterministic, so the
staged driver's 10-restart stall limit aborted (correctly).

Fix in `src/lnvox/tts/staged.py` `_phase_decode`: run the whole vocoder
phase in **fp32 on MPS**. Decode is cheap next to dit and the dit latents
were all cached, so the resume cost was minutes. CUDA keeps `_knobs()`.

### Finding F10 — fp16 Gemma encoder returns all-NaN on MPS → the pipeline "succeeds" with 36 minutes of silent audio (fixed: ctx phase is bf16 on MPS)

**The most dangerous failure of the whole exercise: exit code 0, valid
m4b container, correct chapters — and every audio sample was digital
zero.** The only external tells were the file size (1.1 MB for a "36 min"
m4b ≈ 2.3 kbps ≈ silence) and `volumedetect` reading mean/max −91.0 dB.

Root-cause chain (verified by re-rendering one beat with `--keep-staged`
and inspecting tensors):

- `refs` latents (audio VAE, fp16): healthy — std 1.24, no NaN.
- `ctx` encodings (Gemma prompt-encoder, fp16): **100% NaN**. Gemma is
  bf16-trained and its activations overflow fp16's range — a known Gemma
  property that §11.3's "fp16 sidesteps MPS's weak bf16" default runs
  straight into.
- NaN conditioning → dit output garbage → vocoder emitted zeros. No stage
  raised; the content-hash cache then happily preserved 136 silent WAVs.

Fix in `_phase_ctx`: **bf16 on MPS** for the encoder (torch 2.11 + M4
handles it; fp32 would need ~48 GB). Verified: bf16 encodings are clean
(std ≈1.0, absmax 17 — safely inside fp16 range for the dit hand-off), and
the re-rendered beat has real audio (absmax 0.93, RMS 0.13).

**Doc consequence for §11.3:** "prefer fp16 on MPS" is wrong as a blanket
rule. Working per-phase dtype map on MPS: audio-VAE/refs fp16 ✓, Gemma
encoder **bf16** (fp16 = NaN), dit fp16 ✓, vocoder/decode **fp32**
(fp16 = dtype clash).

**Process consequence:** "renders without error" ≠ audio. `ffprobe` +
`volumedetect` (or just the file size) must be part of any s4 validation;
a listening check remains the final gate.

### Ops fix — caffeinate (user suggestion)

macOS power management throttles/sleeps multi-hour renders.
`run_pipeline.sh` now re-execs itself under `caffeinate -dis` on Darwin
(guarded by `LNVOX_CAFFEINATED` so it happens once); the in-flight render
got `caffeinate -w <pid>` attached retroactively.

### Throughput (M4 Max, staged s4, fp16 dit)

- ctx (Gemma encode): ~2 s/item.
- dit (diffusion render): ~2.1 h wall-clock for ~36.2 min of audio
  (136 beats) ≈ **3.5× real-time** — comfortably inside (better than)
  DESIGN §11.3's 5–10× expectation.
- decode (vocoder, fp32): ~7 min for 136 beats.
- Full re-render in flight to replace the silent cache; final verified
  numbers below.

### Finding F11 — MPS boolean-indexing kernel breaks the DiT attention mask on beats ≳25 s (fixed: MPS chunk cap in the planner)

At 123/136 latents, the `denoise` phase crashed on a ~31 s beat inside
DramaBox's `transformer_args._prepare_self_attention_mask`:
`bias[positive] = torch.log(...)` → `shape mismatch: value tensor of shape
[2585271] cannot be broadcast to indexing result of shape [2586271]` —
**two boolean-index operations over the same mask returning different
counts**, a torch-MPS kernel bug that appears once the (T, T) mask hits
~2.6M elements (beats ≳25 s). 13 long beats were affected; short beats
were untouched. Worth a torch upstream report (torch 2.11, MPS,
`index_put_` / boolean masking on large tensors).

Fix (planner policy, not a DramaBox patch), in two layers on MPS:

1. `build_plan` caps `max_chunk_duration` at 22 s (target 18 s) — mask
   stays ≤~1.3M elements (2× margin), and chunks also duck under
   DramaBox's 20.5 s silence-prior path.
2. The cap alone turned out to be insufficient: DramaBox's
   `chunk_prompt_for_duration` splits on sentence boundaries inside the
   quoted body and returns a long single utterance **whole** — beat
   prompts are `(voice direction) "one long quoted line"`, so a 31 s
   Carroll sentence sailed through the cap untouched (the re-planned run
   still had 136/136 single chunks). Added `_force_split_oversize()`:
   word-boundary split as a last resort, replicating the direction header
   on each piece. The decode phase's equal-power crossfade smooths the
   mid-sentence seam — flag for the listening check. With it, the plan
   went 136 → 174 denoise chunks.

**Design wart exposed** (left as a finding, not refactored): staged
`item_id`s are `{cache_key}_cNN` — chunk boundaries are not part of the
identity, so changing chunk policy silently REUSES stale single-chunk
latents for beats that are now split. For this run the 23 affected
latents were purged by hand; long-term the chunk text (or the chunk
params) belongs in the item id.

### Also observed — the sleep gap

Run 9's dit phase spanned ~8.5 h wall-clock for what is ~4 h of compute;
the machine evidently slept mid-render (before caffeinate was attached).
With `caffeinate -dis` in the launcher this shouldn't recur; noting it so
the 8.5 h number isn't mistaken for MPS throughput.

### Finding F12 — stale ctx reuse across a chunk-policy change: "whole beat fast, then the tail again slow" (fixed: chunk text now part of the item id)

Reported by the user after listening (run 11's m4b): long beats played as
a fast-paced rendition of the **entire** text followed by a slower
rendition of the tail; short beats were perfect.

Mechanism — the F11 "design wart" biting in a second place: when the
word-splitter re-chunked the long beats, their new `c00` **latents** were
purged by hand, but the `c00` **ctx encodings** (same positional
`{cache_key}_c00` naming) were not. Run 11's ctx phase saw them as done
and skipped re-encoding, so `c00` denoised with the OLD full-text
encoding under the NEW ~16 s frame budget — DramaBox crams the whole text
into the window (the fast complete version) — while `c01` was freshly
encoded with only the tail (the slow version).

Fix (permanent): staged `item_id` now embeds a hash of the chunk text
(`{cache_key}_cNN-{sha256(text)[:8]}`) — any future chunk-policy change
auto-invalidates every derived intermediate; no more hand-purges. The 45
affected cache WAVs (>19 s) were evicted and re-rendered clean (run 12,
83 chunks ≈ 1.4 h), and the m4b re-mixed. Duration is unchanged by design
(chunk lengths are pinned by their planned frame budgets; only the speech
content changed). **Listening pass done — see the Phase 4 verdict.**

Note on the user's alternative (split beats in s2 on Mac instead of s4):
now that the artifact is explained by cache identity — not by the split
mechanism itself — the s4 planner split + honest ids is the smaller fix.
Moving the split into s2 remains the better long-term home (LLM picks
semantically sensible seams; both TTS paths benefit; s6 sync spans stay
beat-aligned) but touches stage contracts, so per the project's
design-first convention it's filed under open items rather than done here.

### Finding F13 — decode placement never refreshes existing 05_audio copies; a cache re-render silently doesn't reach the m4b (fixed)

Caught by the user: after F12's re-render the "new" m4b was
**byte-identical** to the corrupt one. The staged decode's placement step
copied cache → `05_audio/<ch>/<beat>.wav` only `if not wav_path.exists()`
— so the 45 freshly re-rendered cache entries never replaced the stale
chapter copies, and s5 mixed the old audio (hash check: 45/136 chapter
wavs differed from their cache sources). Any cache eviction + re-render
was invisible to the final mix.

Fix in `staged.py` placement: refresh when the chapter copy is missing
**or older than its cache source** (mtime ordering self-stabilizes with
the trim-upgrade copy-back). Re-run verified: m4b hash changed, and all
136 chapter wavs now hash-identical to cache. This bug is not Mac-specific
— it affects CUDA too whenever a cache entry is re-rendered. Final m4b:
34:33 (2072.7 s), 25.3 MB.

## Phase 4 — mix & verify (final)

Run 11 (all four MPS fixes active: bf16 encoder, fp32 vocoder, 22 s chunk
cap, word-split fallback) completed the whole TTS + mix with **zero
failures**: 174 denoise chunks (~1.4 h for the 74 uncached ones ≈ 69 s per
≤22 s chunk), decode ~7 min, mix ~1 min.

Final artifact — `artifacts/test/alice/06_final/Alice - MLX smoke test.m4b`:

| check | result |
|---|---|
| size / bitrate | 25.7 MB @ 96 kbps AAC (the silent failure was 1.1 MB) |
| duration | 2103 s = 35:03 |
| chapters | 3, correct titles + boundaries (25 s title page / 17:45 ch. I / 16:49 ch. II) |
| loudness (5 sample points) | mean −19…−22 dB, peaks ≈ −5 dB — consistent with the −18 LUFS mix target |
| exit code | 0 |

**Human listening check (2026-08-03): PASSED.** The user listened and
judges the output **better to the ear than the CUDA/DramaBox renders**
(informal cross-book comparison — this Alice build has no CUDA twin). No
long-beat seam artifacts reported after the F12 re-render.

## Effective throughput (M4 Max, staged s4, final config)

- ctx (bf16 Gemma encode): ~2 s/chunk.
- denoise (fp16 dit, ≤22 s chunks): ~69 s per chunk ≈ **3.6× real-time**.
- decode (fp32 vocoder): ~3 s/chunk.
- Rule of thumb: **~4 h of render per hour of audio** end-to-end on this
  M4 Max — better than DESIGN §11.3's 5–10× expectation, provided the
  machine is kept awake (caffeinate is now built into the launcher).

## Changed files (working tree, no commits — per user instruction)

| file | change |
|---|---|
| `scripts/serve_mlx.sh` | new CLI form; `--temp` (F5); default model → 12B QAT (needs git mlx-lm) |
| `scripts/run_pipeline.sh` | mlx default model; caffeinate re-exec on Darwin |
| `pyproject.toml` / `uv.lock` | git-pinned mlx-lm @ 8239c72 (F7 — drop at next release) |
| `src/lnvox/llm/client.py` | enable_thinking gate widened to mlx (F2); retry temp escalation + retry-prompt feedback (F5) |
| `src/lnvox/tts/dramabox_client.py` | `resolve_gemma_root()` — unquantized encoder when bnb off (F8) |
| `src/lnvox/tts/staged.py` | ctx bf16 on MPS (F10); decode fp32 on MPS (F9); MPS chunk cap + `_force_split_oversize` (F11) |
| `src/lnvox/tts/staged_driver.py` | pass device into `build_plan` |
| `.gitignore` | `logs/` (live-tail symlink `logs/pipeline_current.log`) |
| `MAC_TEST_REPORT.md` | this report |

## Upstream issues worth filing

1. **mlx-lm**: batched path ignores per-request sampling params; per-request
   deterministic sampling defeats retry loops (F5).
2. **mlx-lm**: failed lazy model load leaves the HTTP request hanging
   forever while `/v1/models` stays healthy (F7).
3. **pytorch / MPS**: boolean-index assignment returns inconsistent element
   counts on ~2.6M-element masks, racy across attempts (F11; torch 2.11,
   macOS 26.6, M4 Max).
4. **mlx-community**: Gemma 4 12B conversions all `gemma4_unified`,
   unloadable by every released mlx-lm (F7) — resolved upstream by
   ml-explore/mlx-lm#1349, unreleased.

## Open items (findings, not fixed — design-first per §11.5)

- F4: ingest-epub front-matter filter misses Gutenberg boilerplate pages.
- Beat-length policy: consider splitting over-long beats in **s2** (LLM
  phase) when targeting Mac, so chunk seams land on semantic boundaries
  instead of word counts (user suggestion; needs a stage-contract design
  pass — s3 direction and s6 spans are per-beat).
- The monolithic (non-staged) s4 path on MPS is untested; its equivalents
  of F9/F10 live inside DramaBox's `TTSServer` and would need the same
  dtype treatment routed via `DramaboxClient` kwargs.
- §11.3's dtype table needs updating (fp16 blanket rule is wrong — see F10).

## Listening verdict & CUDA backport candidates (2026-08-03)

The user finds the Mac render **better to the ear** than prior CUDA
renders. The MPS path differs from CUDA in ways that plausibly explain it,
ranked by suspected audible impact:

1. **Shorter render chunks** (F11: ≤22 s cap / 18 s target vs CUDA's
   ~30 s beats). README already documents that shorter beats lower
   DramaBox's noise floor and slurring — likely the dominant factor.
2. **Unquantized bf16 Gemma prompt-encoder** (F8/F10) vs bnb-4bit on
   CUDA — full-precision conditioning should improve direction-following
   and prosody. The staged path makes this portable: the `ctx` phase loads
   ONLY the encoder, so ~24 GB bf16 fits a 32 GB 5090 where the monolithic
   path could not.
3. **fp32 vocoder decode** (F9) vs fp16 — subtle noise-floor gain, and
   decode is cheap (~3 s/chunk), so near-free on CUDA.

Confounds: informal cross-book comparison, different direction text
(different LLM run). A blind A/B on one CUDA-rendered chapter — baseline
vs +chunk-cap vs +bf16-encoder vs both — is the honest test before any
CUDA default changes.

**Cache warning for the A/B:** the s4 content cache key is
(prompt, ref clip, model_version) — encoder precision/checkpoint and
chunk policy are NOT in the key on the monolithic path. Variants must
bump the key (or force re-render) or the A/B silently mixes old and new
audio — the F12/F13 trap in a new coat.

**Blind A/B rendered (2026-08-03, RTX 5090):** the overrides are now
env flags on the staged path — `LNVOX_S4_ENCODER_PRECISION=bf16`
(honored monolithically too), `LNVOX_S4_CHUNK_CAP=<s>`,
`LNVOX_S4_DECODE_DTYPE=fp32` — and any active override appends a
variant tag (e.g. `+enc-bf16+cap22+dec-fp32`) to the cache key, so the
A/B could not reuse baseline renders (monolithic s4 refuses the
staged-only flags rather than mis-keying). Test material: Oz ch. I
(`novels/oz-blind`, 30 beats, one shared LLM pass): baseline = 30
denoise chunks, 522.8 s; mac-quality = 44 chunks, 0 cache reuse,
552.3 s, ~8 min render wall incl. the one-time 23 GB encoder fetch.
Outputs anonymized as `artifacts/oz-blind/06_final/oz-blind-{red,blue}.m4b`
(same embedded title); the mapping was coin-flipped and sealed unseen in
`artifacts/oz-blind/blind_key.b64` — decode with `base64 -d` only after
voting.

**VERDICT (2026-08-03): the mac-quality config WON.** Blind vote: blue
better, "did not hear any artifact contrary to red" — unsealed, blue =
mac-quality (enc-bf16 + cap22 + dec-fp32), red = baseline. So the CUDA
default render has audible artifacts on this material that the backported
config removes. Caveats: n = 1 chapter / 1 listener, and the three
changes were bundled — which one(s) kill the artifacts is untested
(isolating = two more renders: cap-only and enc-only). Cost of the
config on CUDA, measured: **~20% FASTER denoise** (218 s → 174 s —
44 short chunks beat 30 long ones because DiT attention is quadratic
in chunk length), ctx encode comparable (~12 s vs ~10 s), decode/mix
unchanged; the only real costs are the one-time 23 GB encoder download
(+~3 min first load) and the ctx phase's ~24 GB VRAM appetite (fine
staged, risky monolithic). Recommended path: run the next full book
with the three env flags set, and if it holds up, promote them to CUDA
defaults via a deliberate MODEL_VERSION bump (not the variant tag) +
update DESIGN §11.3/§2.6.

## Stale-doc call-outs

- `DESIGN.md §11.2 / serve_mlx.sh` comments name
  `mlx-community/gemma-4-E4B-it-4bit`; HF repo is now lowercase
  `gemma-4-e4b-it-4bit` (redirect makes this cosmetic today).
- `DESIGN.md §11.4` says serve_mlx.sh launches `python -m mlx_lm server` —
  the script actually had the deprecated `mlx_lm.server` module form (now
  fixed to match the doc).
- `client.py` comment claimed "vLLM/mlx-lm templates with no equivalent
  variable silently ignore the field [enable_thinking]" — half-wrong for
  mlx: the mlx-community Gemma 4 template **does** have the variable, defaults
  it **on**, and needs the override (F2).
