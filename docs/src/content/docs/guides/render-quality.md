---
title: Render quality
description: The default Dramabox recipe (bf16 encoder, 22 s chunks, fp32 vocoder), the three LNVOX_S4_* revert knobs, what they cost, and how they re-key the TTS cache.
sidebar:
  order: 8
---

Since 2026-08-04 the staged Stage 4 path renders every book with a
**high-quality recipe** on every platform. The staged path is the launcher
default. This guide explains that recipe, the three environment knobs that
revert parts of it, what each part costs in VRAM, disk and time, and the one
consequence that catches people out: the recipe is part of the TTS cache key.

## The default recipe

| Component | Default | Original v1 recipe |
|---|---|---|
| Gemma-3-12B prompt encoder | **Unquantized bf16** (`unsloth/gemma-3-12b-it`) | bnb-4bit (`unsloth/gemma-3-12b-it-bnb-4bit`) |
| Denoise chunking | **22 s cap / 18 s target**, word-boundary fallback split, 50 ms crossfaded seams | Dramabox chunker, 45 s cap / 37 s target |
| Vocoder (VAE decoder + BWE) | **fp32** | per-device dtype (bf16 on CUDA) |

How this became the default:

1. The recipe first appeared as a set of Apple Silicon workarounds.
   MAC_TEST_REPORT.md F8 is the unquantized encoder, F10 is bf16 for that
   encoder, F9 is the fp32 vocoder, and F11 is the 22 s chunk cap.
2. The resulting Mac render sounded better than the CUDA renders.
3. A blind A/B on an RTX 5090, using chapter I of Oz rendered both ways from one
   LLM pass, picked it: no audible artifacts, where the v1 render had them.
   Denoise was also about **20% faster** (218 s against 174 s), because 44 short
   chunks beat 30 long ones when DiT attention cost is quadratic in chunk
   length.
4. After it held up on full books, it became the staged-path default.

The three changes were tested together. Which of them removes the artifacts was
never isolated.

No flags are needed to get this recipe: `./scripts/run_pipeline.sh <book>` uses
it. The code lives in
[`dramabox_client`](/library/tts/dramabox-client/#s4_encoder_precision) (the
knob readers) and [`staged`](/library/tts/staged/#chunk-caps) (where the knobs
are applied).

## Which path honours which knob

| Knob | Staged (`lnvox s4 --staged`) | Monolithic (`lnvox s4`, CUDA/CPU) | VibeVoice |
|---|---|---|---|
| `LNVOX_S4_ENCODER_PRECISION` | yes, default `bf16` (always `bf16` on MPS) | yes, default **`bnb4`** | ignored |
| `LNVOX_S4_CHUNK_CAP` | yes, default `22` (never above 22 on MPS unless numeric) | explicit values **refused** (exit 2); `none`/`0`/`off` accepted as no-ops | ignored |
| `LNVOX_S4_DECODE_DTYPE` | yes, default `fp32` (always `fp32` on MPS) | explicit values **refused** (exit 2); `auto` accepted as a no-op | ignored |

An empty value (`export LNVOX_S4_CHUNK_CAP=`) is the same as unset on every
path. A value the reader rejects (see the tables below) makes `lnvox s4` exit 2
with the message before any work starts, so `s4_retry.sh` does not replay it.

The monolithic path keeps the v1 recipe as its default. `TTSServer` holds the
encoder, DiT and vocoder in one process, and the bf16 encoder does not fit next
to the other two on a 32 GB card. That path also has no hook for chunking or
decode dtype: Dramabox's `generate_to_file` uses its own 45 s / 37 s chunking.
It therefore refuses the staged-only knobs rather than render v1 audio under a
key that claims otherwise ([Stage 4](/stages/s4-tts/#the-env-knob-refusal)).
On MPS the monolithic path is refused outright (exit 2): its single dtype
renders silence there (MAC_TEST_REPORT.md F10), so use `--staged`, which is
the launcher default.

## The three knobs

All three are read with `.strip().lower()` from the environment of the
`lnvox s4` process. The staged phase subprocesses inherit them. Set them for a
single run, or export them before running the launcher.

### `LNVOX_S4_ENCODER_PRECISION`

| Value | Meaning |
|---|---|
| unset or empty | `bf16` on the staged path, `bnb4` on the monolithic path |
| `bf16` | Unquantized encoder, from `huggingface_hub.snapshot_download("unsloth/gemma-3-12b-it")` |
| `bnb4` | Dramabox's pre-quantized 4-bit encoder. On MPS the staged path still loads `bf16`: bitsandbytes is CUDA-only (F8). |
| anything else | `ValueError`; `lnvox s4` exits 2 |

The cost of `bf16`:

- a one-time download of about 23 to 24 GB into the HF cache, on top of
  Dramabox's own bnb-4bit copy;
- about 3 extra minutes on first load;
- about 24 GB of VRAM during the `ctx` phase. That phase loads nothing else, so
  it fits a 32 GB card.

The `bnb4` encoder is about 8 GB (DESIGN.md §15.1). It is lighter on disk and
VRAM, but its conditioning is audibly worse.

### `LNVOX_S4_CHUNK_CAP`

| Value | Meaning |
|---|---|
| unset or empty | `22` s cap, target `18` s |
| a number, e.g. `30` | cap = that many seconds, target = `max(6, cap - 4)` |
| `0`, `none`, `off` | CUDA/CPU: Dramabox's 45 s / 37 s chunker, no forced word split. MPS: still 22 s / 18 s with the word split, because that cap is the F11 crash guard. |
| non-numeric or negative | `ValueError`; `lnvox s4` exits 2 |

While a cap is active, which is the default, beats whose estimate exceeds the
cap are first split by Dramabox's sentence chunker. Any piece that is still too
long is then split at word boundaries, and the `(voice direction)` header is
repeated on each part. The decode phase crossfades the seams (50 ms), but a
word-split seam can fall mid-sentence.

- Shorter chunks mean more denoise items: 44 instead of 30 on the Oz chapter.
  They also mean more ctx and latent intermediates under `_staged/`, which are
  freed as each render decodes.
- The DiT runs faster on short chunks.
- `none` restores the v1 chunking. In the blind test that was the source of the
  artifacts, and it is also slower.

### `LNVOX_S4_DECODE_DTYPE`

| Value | Meaning |
|---|---|
| unset or empty | `fp32` vocoder |
| `fp32` | fp32 vocoder |
| `auto` | Per-device dtype from `staged._knobs()`: bf16 on CUDA and CPU. MPS forces fp32 anyway (F9). |
| anything else | `ValueError`; `lnvox s4` exits 2 |

Decode is cheap (about 3 s per chunk on an M4 Max), so fp32 costs very little
time. The gain is a subtle one in the noise floor.

## Cache-key consequence

The TTS cache key is `sha256(prompt, ref clip filename, model_version)`. On the
staged path, `model_version` is
`DramaboxClient.MODEL_VERSION + s4_variant_tag(device)`
([details](/library/tts/dramabox-client/#s4_variant_tag)). The tag records the
**effective** recipe on the render device, as a deviation from the **original
v1 recipe**, not from today's defaults. On CUDA and CPU every knob takes effect:

| Staged settings on CUDA/CPU (encoder / cap / decode) | Tag | Same keys as |
|---|---|---|
| defaults (`bf16` / `22` / `fp32`) | `+enc-bf16+cap22+dec-fp32` | renders made while these were opt-in flags, and default Mac renders |
| `bnb4` / `none` / `auto` | `""` | a default monolithic render, or a v1 render from before 2026-08-04 |
| `bf16` / `none` / `auto` | `+enc-bf16` | a monolithic render with `LNVOX_S4_ENCODER_PRECISION=bf16` |
| `bf16` / `none` / `fp32` | `+enc-bf16+dec-fp32` | nothing else |
| `bnb4` / `22` / `fp32` | `+cap22+dec-fp32` | nothing else |
| `bf16` / `30` / `fp32` | `+enc-bf16+cap30+dec-fp32` | nothing else |

On MPS the reverts cannot take effect (bf16 encoder, 22 s cap and fp32 decode
are forced, see below), so they do not change the tag either:

| Staged settings on MPS | Tag |
|---|---|
| defaults, or any mix of `bnb4` / `none` / `auto` | `+enc-bf16+cap22+dec-fp32` |
| an explicit cap, e.g. `30` (with any encoder / decode value) | `+enc-bf16+cap30+dec-fp32` |

Before this was fixed, the Mac tag followed the raw knobs: `bnb4` gave
`+cap22+dec-fp32`, `CHUNK_CAP=none` gave `+enc-bf16+dec-fp32` (the key CUDA
uses for real 45 s chunks) and all three reverts gave `""`. Mac cache entries
filed under those keys hold default-recipe audio; they are no longer looked up.

The consequences:

- Renders made with different recipes **never collide** in `cache/tts/`, so an
  A/B cannot accidentally reuse the other variant's audio.
- **Changing the recipe mid-book re-renders the whole book**, because every key
  changes. Switching between the staged and monolithic paths at their defaults
  does the same. Pick one recipe per book and keep it.
- Entries left from an old recipe stay in `cache/tts/` and are never reused.
  Delete them by hand if disk is tight.
- To check which recipe a run is using, look at the `Render recipe:` line the
  staged driver prints after the plan line: it is the full `model_version`,
  tag included. A re-run of an already rendered book should also show
  `(K already cached)` with `K` close to the number of renders.
  `"model_version"` in `artifacts/<book>/05_audio/_staged/plan.json` holds the
  same value while a run is in progress, or afterwards if you used
  `--keep-staged`.

:::note[What MPS forces]
Whatever the knobs say, on MPS the ctx phase is always bf16 and unquantized
(bnb is CUDA-only, and fp16 Gemma is all-NaN), decode is always fp32, and
chunks stay at 22 s / 18 s with the word split unless you set an explicit
numeric cap. The revert knobs are therefore no-ops on a Mac, and its default
key is the same as a CUDA box's default key. The v1 recipe cannot be
reproduced on MPS. An explicit cap above about 25 s on MPS brings back the F11
crash.
:::

## A/B-ing recipes on one chapter

```bash
# Variant A: default recipe  →  artifacts/mybook/06_final/ab-a.m4b
uv run lnvox s4 mybook --staged --chapters 01
uv run lnvox s5 mybook --chapters 01 --title ab-a

# Variant B: v1 recipe (different keys, so nothing is reused)  →  ab-b.m4b
LNVOX_S4_ENCODER_PRECISION=bnb4 LNVOX_S4_CHUNK_CAP=none LNVOX_S4_DECODE_DTYPE=auto \
  uv run lnvox s4 mybook --staged --chapters 01
uv run lnvox s5 mybook --chapters 01 --title ab-b
```

A variant's `05_audio/<chapter>/` placement is replaced by the next variant's
run, so mix each variant right after rendering it. The m4b embeds its title. For a blind
test, mix both with the same `--title` and rename the first m4b before you mix
the second, because the second would overwrite it.

Switching back and forth is safe: the staged `_finalize` re-places every
`05_audio/<chapter>/<beat>.wav` that is not an exact copy of the cache entry
the manifest names ([placement](/library/tts/staged/#_finalize)). Going back
to recipe A re-copies A's cached WAVs, even though they are older than B's
placed files, and costs no render.

## Choosing a recipe

| Situation | Suggestion |
|---|---|
| 32 GB or larger CUDA card, or Apple Silicon | Defaults. Nothing to set. |
| Smaller CUDA card, or tight disk | `LNVOX_S4_ENCODER_PRECISION=bnb4`. Keep the cap and fp32 decode, which cost almost nothing. |
| Reproduce, or keep reusing, a render from before 2026-08-04 | CUDA/CPU only: all three reverts on the staged path, or `LNVOX_S4_STAGED=0` for the monolithic path. The tag is `""` either way. |
| Continue a book that was started with explicit opt-in flags | Defaults. Their tag is byte-identical. |

To change the sampling parameters themselves (`cfg_scale`, `stg_scale`,
`duration_multiplier`, `seed`), edit `DramaboxClient.DEFAULT_PARAMS` and bump
`MODEL_VERSION`. There is no environment knob for them.

Related: [Stage 4](/stages/s4-tts/), [Apple Silicon](/guides/apple-silicon/),
[staged internals](/library/tts/staged/#per-component-dtype-map),
[troubleshooting](/guides/troubleshooting/).
