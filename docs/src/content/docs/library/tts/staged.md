---
title: lnvox.tts.staged
description: The four checkpointed Dramabox phases (refs, ctx, denoise, decode) - plan model, build_plan, chunk caps, per-component dtype map, _staged/ layout.
sidebar:
  order: 3
  label: staged
---

`lnvox.tts.staged` implements the staged Stage 4 render (DESIGN.md §15).
Dramabox's `TTSServer.generate()` is split into four GPU phases. Each phase
loads exactly **one** model, sweeps every pending work item, and writes its
result to disk. The driver ([staged_driver](/library/tts/staged-driver/)) builds
the plan and runs each phase as a separate `lnvox s4-phase` subprocess.
[`run_phase`](#run_phase) is that subprocess's entry point.

This is the default Dramabox path in the launcher. Every phase body mirrors
`external/DramaBox/src/inference_server.py` step for step: same
conditioning-before-noise order, same guider config, same frame-513
silence-prior fix. With the same recipe, outputs are numerically identical to
the monolithic path, modulo kernel nondeterminism. Dramabox's source is never
patched; its building blocks are imported and composed here.

## At a glance

| Name | Purpose |
|---|---|
| `PHASES` | `("refs", "ctx", "denoise", "decode")`, the run order. |
| `PlanParams`, `PlanChunk`, `PlanRender`, `PlanBeat`, `PlanRefClip`, `StagedPlan` | The plan model, serialized to `_staged/plan.json`. |
| `build_plan(...)` | CPU-only, deterministic. Turns directed chapters into renders, chunks and refs. |
| `apply_chunk_policy(params, device)`, `chunk_texts(...)` | The per-device chunk policy. Shared with the [lab engine](/library/tts/lab-engine/). |
| `staged_root`, `load_plan`, `write_plan`, `mark_inflight` | Paths and bookkeeping. |
| `pending_refs` / `pending_ctx` / `pending_latents` / `pending_renders` / `pending_placements`, `count_pending`, `first_pending` | "What is left" calculators, shared by the driver and the phases. |
| `_knobs(device)` | Per-device `(dtype, bnb_4bit, compile_model)`. |
| `denoise_chunk(...)` | One chunk through the Euler recipe. Shared with the [lab engine](/library/tts/lab-engine/). |
| `_finalize(...)` | Places beat WAVs from the cache, trims them, writes the per-chapter `manifest.json`. |
| `run_phase(phase, book_dir, cache_dir, device)` | Runs one phase sweep. |

## Phases

| Phase | Model loaded | Reads | Writes (atomic) |
|---|---|---|---|
| `refs` | `AudioConditioner` (VAE encoder), plus RE-USE denoiser when available | voicebank clip WAVs | `_staged/refs/<ref_hash>.safetensors` (`ref_latent`) |
| `ctx` | `PromptEncoder` (Gemma-3-12B) | chunk texts, Dramabox `DEFAULT_NEG` | `_staged/ctx/<item_id>.safetensors` (`a_ctx`), `_staged/ctx/neg.safetensors` once per book |
| `denoise` | LTX audio-only DiT | ref latents, ctx, neg | `_staged/latents/<item_id>.safetensors` (`latent`) |
| `decode` | `AudioDecoder` (VAE decoder + BigVGAN BWE) | latents | `cache/tts/<cache_key>.wav`. Deletes that render's ctx and latents, then calls `_finalize` to write `05_audio/<chapter>/<beat_id>.wav` and `manifest.json`. |

"Done" always means the output file exists. Writes go to a temp name followed
by `os.replace`: `<name>.tmp` for JSON and tensors, `<stem>.tmp.wav` for WAVs.
A crashed phase resumes by listing what is still pending, and nothing is ever
skipped. Crash policy belongs to the driver.

## `_staged/` storage layout

```text
artifacts/<book>/05_audio/
├── _staged/                          # staged_root(book_dir); removed on success unless --keep-staged
│   ├── plan.json                     # StagedPlan, rebuilt every run
│   ├── inflight.json                 # {"phase", "item", "ts"} of the item being attempted
│   ├── attempts.json                 # "<phase>:<item>" → crash count (driver, diagnostics only)
│   ├── refs/<ref_hash>.safetensors   # one per unique voice clip
│   ├── ctx/<item_id>.safetensors     # one per chunk; deleted when its render is decoded
│   ├── ctx/neg.safetensors           # negative prompt, encoded once per book (cfg_scale > 1)
│   └── latents/<item_id>.safetensors # one per chunk; deleted when its render is decoded
└── <chapter_id>/
    ├── <beat_id>.wav
    └── manifest.json
cache/tts/<cache_key>.wav             # relative to the working directory
```

- `ref_hash` is `sha256(clip bytes)[:16]`, the file content rather than the
  filename.
- `item_id` is `f"{cache_key}_c{NN:02d}-{sha256(chunk_text)[:8]}"`. The chunk
  **text** is part of the id, so a change in chunking policy invalidates every
  derived intermediate (MAC_TEST_REPORT.md F12).

DESIGN.md §15.5 sizes the intermediates for a novel of about 5,000 items: refs
about 50 MB, ctx 5 to 10 GB (the dominant term, freed per render as decode
lands), latents about 1 GB.

## API

### `PHASES`

```python
PHASES = ("refs", "ctx", "denoise", "decode")
```

The driver runs the phases in this order. `run_phase` and `lnvox s4-phase`
reject any other name.

### `PlanParams`

```python
class PlanParams(BaseModel)
```

The sampling and chunking recipe. `build_plan` constructs it as
`PlanParams(**DramaboxClient.DEFAULT_PARAMS)` and then applies the chunk caps
with [`apply_chunk_policy`](#apply_chunk_policy).
The defaults mirror `TTSServer.generate` and `generate_long`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `cfg_scale` | `float` | `2.5` | Classifier-free guidance scale. Values above 1.0 require the negative context. |
| `stg_scale` | `float` | `1.5` | STG guidance scale, applied at transformer block 29 (`stg_blocks=[29]`). |
| `duration_multiplier` | `float` | `1.1` | Applied to the speech-duration estimate. |
| `seed` | `int` | `42` | Seed for `torch.Generator(...).manual_seed`. |
| `denoise_ref` | `bool` | `True` | Run RE-USE denoise on voice refs. |
| `watermark` | `bool` | `False` | Apply the Perth watermark at decode. |
| `ref_duration` | `float` | `10.0` | Seconds of the ref clip used, tiled if shorter. |
| `steps` | `int` | `30` | Euler denoise steps. |
| `fps` | `float` | `25.0` | Latent frame rate used for frame sizing. |
| `max_chunk_duration` | `float` | `45.0` | Chunker cap before overrides. The effective default is 22 s (see [Chunk caps](#chunk-caps)). |
| `target_chunk_duration` | `float` | `37.0` | Chunker target before overrides. The effective default is 18 s. |
| `crossfade_ms` | `float` | `50.0` | Equal-power crossfade at chunk seams. |

### Plan models

```python
class PlanChunk(BaseModel)    # item_id: str, text: str, gen_duration: float, n_frames: int
class PlanRender(BaseModel)   # cache_key: str, prompt: str, ref_hash: Optional[str] = None,
                              # cached: bool = False, chunks: list[PlanChunk] = []
class PlanBeat(BaseModel)     # beat_id, scene_id, chapter_id, type, speaker, cache_key: str
class PlanRefClip(BaseModel)  # ref_hash: str, clip_path: str  (absolute)
class StagedPlan(BaseModel)   # book_id: str, model_version: str, params: PlanParams,
                              # refs: list[PlanRefClip] = [], renders: list[PlanRender] = [],
                              # beats: list[PlanBeat] = []
```

- **`PlanRender`** is one unique `(prompt, voice, recipe)` render, keyed by
  `cache_key`. Several beats can share it. `cached=True` means the cache WAV
  already existed at plan time. In that case `chunks` is empty and no phase
  works on the render.
- **`PlanChunk`** is one denoise-sized unit: either a whole short beat or one
  slice of a long one. `gen_duration` is the per-chunk duration estimate.
  `n_frames` is the latent length computed from it.
- **`PlanBeat`** places a render into the book, in playback order.
- **`StagedPlan.model_version`** is the full effective version, e.g.
  `dramabox-dit-v1-cfg2.5-stg1.5-dm1.1+enc-bf16+cap22+dec-fp32`. It is the
  quickest way to check which recipe a run used.

### `staged_root`

```python
def staged_root(book_dir: Path) -> Path
```

Returns `book_dir / "05_audio" / "_staged"`.

### `load_plan` / `write_plan`

```python
def load_plan(root: Path) -> StagedPlan
def write_plan(root: Path, plan: StagedPlan) -> None
```

Read and write `root / "plan.json"`. The write is atomic and uses
`indent=2`.

### `mark_inflight`

```python
def mark_inflight(root: Path, phase: str, item_id: str) -> None
```

Called just before each item is attempted. It atomically writes
`inflight.json` = `{"phase": ..., "item": ..., "ts": time.time()}`. The driver
reads it after a crash to attribute the crash to an item. It is diagnostics
only and is never used to skip anything.

### `build_plan`

```python
def build_plan(
    book_id: str,
    chapters,
    casting,
    voicebank,
    voicebank_root: Path,
    cache_dir: Path,
    limit: Optional[int] = None,
    device: str = "cuda",
) -> StagedPlan
```

Builds the whole book's work list on the CPU. No torch is imported: the
duration estimator is reimplemented so planning does not import
`inference_server`.

1. `_ensure_ltx_path()`, so Dramabox's `text_chunker` and
   `duration_estimator` import.
2. `params = PlanParams(**DramaboxClient.DEFAULT_PARAMS)`, then
   `force_split = apply_chunk_policy(params, device)` (the
   [chunk caps](#chunk-caps)).
3. `model_version = DramaboxClient.MODEL_VERSION + s4_variant_tag(device)`.
   The tag describes the **effective** recipe on `device`, so on MPS the
   revert knobs, which cannot take effect there, do not re-key the cache.
4. `speaker_to_clip = s4_tts._build_speaker_to_clip_path(...)`. This raises if
   the casting references clip ids missing from the loaded voicebank.
5. With `limit` set, keep only the first `limit` beats overall (the same slicing
   as `s4_tts.slice_chapters`).
6. For each beat, in chapter, scene, beat order:
   - `beat_id = f"{scene_id}_b{idx:04d}"`.
   - `cache_key = _content_hash(beat.prompt, clip.name or "no-ref",
     model_version)`. This is the same key recipe as the monolithic path.
   - The first time a clip is seen, hash its bytes into a `PlanRefClip`.
   - Append a `PlanBeat`. If this `cache_key` already has a render, stop here
     (dedupe).
   - Check whether `cache_dir/<cache_key>.wav` exists. If it does not, split
     the prompt with [`chunk_texts`](#chunk_texts). Each piece becomes a
     `PlanChunk` with its own duration estimate and frame count.

**Returns** the `StagedPlan`. The driver writes it to `plan.json`.

### `apply_chunk_policy`

```python
def apply_chunk_policy(params: PlanParams, device: str) -> bool
```

Sets `params.max_chunk_duration` / `target_chunk_duration` to the effective
cap for `device` ([`s4_effective_chunk_cap`](/library/tts/dramabox-client/#effective-recipe-helpers))
and returns whether the word-split fallback applies, which is exactly when a
cap is enforced. With a cap: max = cap, target = `max(6.0, cap - 4.0)`.
Without one (`LNVOX_S4_CHUNK_CAP=none` on CUDA or CPU), `params` keeps
Dramabox's 45 s / 37 s and it returns `False`. `build_plan` and
[StagedLabEngine](/library/tts/lab-engine/) both call it, so the lab chunks a
prompt the way a book render does.

### `chunk_texts`

```python
def chunk_texts(
    prompt: str, params: PlanParams, duration_multiplier: float, force_split: bool
) -> list[str]
```

Splits one prompt into chunk texts. The caller must have run
`_ensure_ltx_path()`.

- If `_estimate_duration(prompt) > max_chunk_duration`, use Dramabox's
  `chunk_prompt_for_duration(max_duration_s, target_duration_s,
  duration_multiplier)`; otherwise `[prompt]`.
- With `force_split` (the return value of `apply_chunk_policy`), pass every
  piece through `_force_split_oversize` as a last resort.

### `_force_split_oversize`

```python
def _force_split_oversize(
    text: str,
    max_duration_s: float,
    target_duration_s: float,
    duration_multiplier: float,
) -> list[str]
```

Word-boundary splitting for text that `text_chunker` returned still over the
cap. `text_chunker` only splits on sentence boundaries inside the quoted body,
so a beat shaped `(voice direction) "one long quoted line"` comes back whole
(MAC_TEST_REPORT.md F11).

- If the estimate is within `max_duration_s`, it returns `[text]`.
- Otherwise it strips a leading `(...)` direction header and one layer of
  surrounding quotes (`"` or curly quotes), and splits the words into
  `max(2, ceil(est / target_duration_s))` roughly equal parts.
- Each part gets the header back and is re-quoted, so every piece is still a
  well-formed beat prompt.
- With fewer than 2 words it returns `[text]` unchanged, and the render fails
  loudly downstream if the text is still too long.

The decode crossfade smooths the seams, which fall mid-sentence.

### `_estimate_duration` / `_n_frames`

```python
def _estimate_duration(prompt: str, multiplier: float) -> float
def _n_frames(gen_duration: float, fps: float) -> int
```

`_estimate_duration` returns
`max(3.0, round(estimate_speech_duration(prompt) * multiplier, 1))`, using
Dramabox's `duration_estimator`. `_n_frames` mirrors `TTSServer.generate()`:
`n = round(d * fps) + 1`, rounded to the next `8k + 1`.

Dramabox sizes the latent from this **estimate**. Any overshoot renders as
silence, which is why [trim](/library/tts/trim/) exists.

### Pending-work calculators

```python
def pending_refs(plan: StagedPlan, root: Path, cache_dir: Path) -> list[str]
def pending_ctx(plan: StagedPlan, root: Path, cache_dir: Path) -> list[PlanChunk]
def pending_latents(plan: StagedPlan, root: Path, cache_dir: Path) -> list[tuple[PlanRender, PlanChunk]]
def pending_renders(plan: StagedPlan, root: Path, cache_dir: Path) -> list[PlanRender]
def pending_placements(plan: StagedPlan, root: Path, cache_dir: Path) -> list[str]
def count_pending(phase: str, plan: StagedPlan, root: Path, cache_dir: Path) -> int
def first_pending(phase: str, plan: StagedPlan, root: Path, cache_dir: Path) -> Optional[str]
```

All of them except `pending_placements` look only at **live** renders,
meaning renders whose `cache/tts/<cache_key>.wav` does not exist yet. A render
that finished (or was cached) never asks for refs, ctx or latents again, even
if those files were deleted.

- `pending_refs`: sorted ref hashes needed by live renders whose ref file is
  missing.
- `pending_ctx`: live chunks without a ctx file. `count_pending("ctx")` adds 1
  when the negative prompt is still needed (`cfg_scale > 1.0`, at least one
  live render, and no `neg.safetensors`).
- `pending_latents`: live `(render, chunk)` pairs without a latent file.
- `pending_renders`: the live renders themselves.
- `pending_placements`: the decode phase's other outputs that do not reflect
  the plan yet. `<chapter>/<beat_id>.wav` for a placed WAV that is not an
  exact copy of its cache entry (the rule in [`_finalize`](#_finalize)), and
  `<chapter>/manifest.json` for a manifest that is missing, lists other beats
  or cache keys than the plan, or records a duration that differs from the
  placed WAV. Existence alone is not enough: a manifest left by an earlier
  run would pass.
- `count_pending("decode")` is `len(pending_renders) + len(pending_placements)`.
  So a decode subprocess that dies inside `_finalize`, after every cache WAV
  was written, still counts as pending and the driver re-runs it.
- `first_pending`: the id the driver names in a stall error. That is a ref hash
  (refs), an item id or `"neg"` (ctx), an item id (denoise), or a cache key or
  `"placement <chapter>/<file>"` (decode).
- `count_pending` raises `ValueError` for an unknown phase.

### `_knobs`

```python
def _knobs(device: str)  # -> (torch_dtype, bnb_4bit, compile_model)
```

Returns `(torch.float16, False, False)` on MPS and
`(torch.bfloat16, True, True)` everywhere else (DESIGN.md §11.3); CUDA keeps
`TTSServer`'s defaults. Individual phases then override the dtype per
component, as in the table below. The encoder's quantization and the decode
dtype come from the [effective-recipe helpers](/library/tts/dramabox-client/#effective-recipe-helpers),
the same functions the cache tag is built from.

### Per-component dtype map

This table is the reason the staged path runs on Apple Silicon at all. On
Metal, no single dtype works for all four models (MAC_TEST_REPORT.md F8 to
F10).

| Component (phase) | CUDA | MPS | CPU |
|---|---|---|---|
| VAE encoder (`refs`) | bf16 | fp16 | bf16 |
| Gemma encoder (`ctx`) | bf16, **unquantized** by default; bnb-4bit if `LNVOX_S4_ENCODER_PRECISION=bnb4` | **bf16**, forced, unquantized (fp16 Gemma overflows to all-NaN, F10) | bf16, unquantized by default |
| DiT (`denoise`) | bf16 + `torch.compile(mode="default", dynamic=True)` | fp16, eager | bf16 + `torch.compile` |
| VAE decoder + BWE (`decode`) | **fp32** by default; bf16 if `LNVOX_S4_DECODE_DTYPE=auto` | **fp32**, forced (fp16 hits the float/half bias clash, F9) | fp32 by default; bf16 if `auto` |

Other details:

- ctx and neg tensors are cast to the DiT dtype when they are loaded for
  denoise. On MPS this is required: mixing bf16 and fp16 operands trips a Metal
  matmul assertion.
- CPU is not special-cased and follows the CUDA column. It is untested for
  Dramabox.
- The Gemma checkpoint comes from
  [`resolve_gemma_root`](/library/tts/dramabox-client/#resolve_gemma_root).
  With bnb off, that is the unquantized `unsloth/gemma-3-12b-it`.

### Phase bodies

```python
def _phase_refs(plan: StagedPlan, root: Path, cache_dir: Path, device: str) -> None
def _phase_ctx(plan: StagedPlan, root: Path, cache_dir: Path, device: str) -> None
def _phase_denoise(plan: StagedPlan, root: Path, cache_dir: Path, device: str) -> None
def _phase_decode(plan: StagedPlan, root: Path, cache_dir: Path, device: str) -> None
```

Each body returns without loading a model when it has nothing pending.
Exception: decode always calls `_finalize`.

- **refs**: `decode_audio_from_file(clip, dev, 0.0, ref_duration)`, then
  optional RE-USE denoise (`_denoise_ref`), then `_prep_ref_waveform` (mono to
  stereo, tile to `ref_duration`, peak-normalize to -4 dBFS), then VAE encode.
  If RE-USE cannot be built, it logs a warning and uses the ref un-denoised.
  This is the normal case on macOS, where `resampy` and `mamba-ssm` have no
  wheels.
- **ctx**: encodes the negative prompt once (when needed), then each chunk
  text with `streaming_prefetch_count=None`, one at a time. Progress prints
  every 10 items. This phase is the only one that loads the encoder, so the
  roughly 23 GB bf16 checkpoint has the GPU to itself.
- **denoise**: sorts pending items by `n_frames`, so that
  `torch.compile(dynamic=True)` sees few distinct shapes. It builds the DiT
  once (`_build_dit`, a copy of `TTSServer._load_all`'s audio-only
  configurator), caches ref latents per `ref_hash`, and calls `denoise_chunk`
  for each item.
- **decode**: decodes each chunk latent and joins chunks with Dramabox's
  `_equal_power_crossfade(fade_ms=crossfade_ms)`. Mono and stereo mismatches
  are broadcast; a sample-rate mismatch raises. Optionally applies the Perth
  watermark, then writes `cache/tts/<cache_key>.wav` and deletes that render's
  ctx and latent files. It then calls `_finalize`. A render with no chunks and
  no cache WAV raises `RuntimeError` and asks you to re-plan with
  `lnvox s4 --staged`.

### `denoise_chunk`

```python
def denoise_chunk(
    *,
    velocity_model,
    patchifier,
    n_frames: int,
    a_ctx,
    a_ctx_neg,
    ref_latent,
    params,
    dev,
    dtype,
)
```

Runs one chunk through `TTSServer`'s Euler recipe and returns the
unpatchified latent. The caller must hold `torch.inference_mode()`. Steps:

1. Build an `AudioLatentShape` from `VideoPixelShape(batch=1,
   frames=n_frames, height=64, width=64, fps=params.fps)`.
2. Apply `AudioConditionByReferenceLatent(strength=1.0)` when `ref_latent` is
   not `None`. Conditioning comes **before** noise.
3. Add Gaussian noise from `torch.Generator(device=dev).manual_seed(params.seed)`.
4. Run the guided Euler loop: `MultiModalGuider` with `cfg_scale`,
   `stg_scale`, `stg_blocks=[29]`, `rescale_scale=auto_rescale_for_cfg(cfg)`,
   `modality_scale=1.0`, and `negative_context=a_ctx_neg`, over
   `LTX2Scheduler` sigmas for `params.steps` steps.
5. Clear the conditioning and unpatchify.
6. Apply the frame-513 fix. When the latent has more than 513 frames, frames
   512 and 513 are replaced by linear interpolation between frames 511 and
   514, which removes Dramabox's end-of-clip silence prior.

`params` is any object with `fps`, `seed`, `cfg_scale`, `stg_scale` and
`steps` (a `PlanParams`). Pass `a_ctx_neg=None` when `cfg_scale <= 1`. This
function is shared with `_phase_denoise` so that
[StagedLabEngine](/library/tts/lab-engine/) runs exactly the same denoise path.

### `_finalize`

```python
def _finalize(plan: StagedPlan, root: Path, cache_dir: Path) -> None
```

Places every planned beat and writes one manifest per chapter. It uses the same
layout and schema as the monolithic `s4_tts.render_chapter`
([ChapterAudio](/library/tts/schema/)). For each beat:

1. A missing `cache/tts/<key>.wav` raises `RuntimeError`.
2. Copy the cache entry to `05_audio/<chapter>/<beat_id>.wav` unless the
   placed file is already an **exact copy** of it. Copies use `shutil.copy2`
   through a `<stem>.tmp.wav` plus `os.replace`, which carries the size and
   mtime over, so "exact copy" means the same size and an mtime within 2 s.
   The slack only absorbs a coarser timestamp grid when `cache/` and the
   artifacts dir are on different filesystems (FAT stores 2 s steps).
3. `trim_file(wav_path)`. When it actually removes silence, the trimmed file is
   copied back over the cache entry the same way, so the pair still matches
   and the next run copies nothing.
4. Measure the duration and append a `RenderedBeat`. `cached` comes from the
   plan.

The exact-copy rule means the file in `05_audio/` is always the cache entry
the manifest names:

- a cache entry re-rendered under the same key reaches the chapter directory.
  Under the original "exists, so skip" rule it never did, and s5 mixed the
  stale audio (MAC_TEST_REPORT.md F13);
- a switch back to an **earlier** casting, direction or recipe re-copies that
  older cache entry. The interim "refresh when the cache is newer" rule left
  the newer audio in place while the manifest named the reverted key;
- a file written by another path (a monolithic run) is replaced.

An unchanged re-run copies nothing. The first staged run after upgrading
re-copies each placed WAV once, because older placements were made with a
plain copy and a fresh mtime; no rendering is involved.

Before each chapter it calls `mark_inflight(root, "decode",
"finalize:<chapter>")`, so a crash here is attributed to a chapter.
`manifest.json` is written atomically, and the phase prints
`[decode] ✓ <chapter>: N beats, T s → manifest.json` for each chapter.

### `run_phase`

```python
def run_phase(phase: str, book_dir: Path, cache_dir: Path, device: str) -> None
```

Entry point for the hidden `lnvox s4-phase <phase> <book_id> [--device]`
command. It validates `phase` against `PHASES` (raising `ValueError`
otherwise), loads `plan.json` from `staged_root(book_dir)`, and dispatches to
the phase body. Each call runs in a fresh process, so it gets a fresh CUDA
context.

## Chunk caps

[`apply_chunk_policy`](#apply_chunk_policy) sets `max_chunk_duration` and
`target_chunk_duration` from
[`s4_effective_chunk_cap(device)`](/library/tts/dramabox-client/#effective-recipe-helpers):
`LNVOX_S4_CHUNK_CAP` (default `22.0`), except that an opt-out on MPS still
gives 22. A cap means max = `cap`, target = `max(6.0, cap - 4.0)`, and the
word-split fallback. No cap means Dramabox's 45 s / 37 s and no forced split.

| Device | `LNVOX_S4_CHUNK_CAP` | Max / target | Word split | Tag part |
|---|---|---|---|---|
| any | unset or empty | 22 s / 18 s | yes | `cap22` |
| CUDA / CPU | `none`, `0`, `off` | 45 s / 37 s (v1) | no | none |
| MPS | `none`, `0`, `off` | 22 s / 18 s | yes | `cap22` |
| any | a number, e.g. `30` | 30 s / 26 s | yes | `cap30` |

On MPS the 22 s cap is a hard requirement, not a quality preference, which is
why an opt-out cannot lift it. An explicit number still overrides it.

Why 22 s: on MPS, torch's boolean-index assignment inside Dramabox's
self-attention mask (`bias[positive] = ...`) returns inconsistent element
counts once the `(T, T)` mask reaches about 2.6M elements, which happens on
beats of roughly 25 s or more (F11). 22 s keeps the mask near 1.3M elements, a
2x margin. Chunks near the 18 s target also stay below frame 513 (20.5 s at
25 fps), the point past which Dramabox's end-of-clip silence prior applies. On CUDA the same 22 s cap won a blind A/B and made denoise
about 20% faster, because DiT attention cost is quadratic in chunk length
([render quality](/guides/render-quality/)).

:::caution[Explicit caps above ~25 s on MPS]
An explicit `LNVOX_S4_CHUNK_CAP` replaces the MPS 22/18 guard. Values that let
chunks grow past about 25 s put long beats back into the F11 failure zone. The
denoise phase then crashloops on the same item until the stall limit aborts.
:::

## Notes

- **Silent success is possible.** NaN conditioning does not raise; it produces
  digital-zero WAVs with exit code 0 (F10). After any device or dtype change,
  check the output with `ffprobe` / `volumedetect`, or at least look at the
  m4b size, before trusting a run. `--keep-staged` keeps the tensors so you can
  inspect them.
- **Fully cached books** still run decode once: `_finalize` re-places and
  re-trims every beat and rewrites the manifests without loading any model.
- **The plan is derived state.** It is rebuilt at the start of every staged
  run. Because intermediates are content-keyed (text hash in the `item_id`,
  byte hash in `ref_hash`), a rebuilt plan reuses everything that is still
  valid.
- **Cross-path resume.** Staged and monolithic runs share `cache/tts/` and the
  cache-key recipe. Beats rendered on one path are reused by the other only
  when the [variant tags](/library/tts/dramabox-client/#s4_variant_tag) match.
  With the current defaults they do not match (`+enc-bf16+cap22+dec-fp32` vs
  `""`). The monolithic path refuses MPS, so on a Mac only staged renders
  exist.
