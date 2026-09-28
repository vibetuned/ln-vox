---
title: lnvox.tts.lab_engine
description: StagedLabEngine - warm, in-process Dramabox engine for the Studio TTS Lab on Apple Silicon, built from the staged components and per-component dtypes.
sidebar:
  order: 7
  label: lab_engine
---

`lnvox.tts.lab_engine` provides `StagedLabEngine`, the Dramabox engine that the
Voicebank Studio's TTS Lab uses **on MPS**. It exposes the same
`generate()` contract as [`DramaboxClient`](/library/tts/dramabox-client/#generate).
Internally it keeps the [staged](/library/tts/staged/) components warm in one
process, each in the dtype that works on Metal, so interactive auditioning is
possible on a Mac. On CUDA the Studio still uses `DramaboxClient`.

:::note[Shared denoise path]
The engine calls `staged.denoise_chunk`, the same function the staged s4
`denoise` phase uses, so the Lab and a book render run identical code
(MAC_TEST_REPORT.md addendum, 2026-08-22).
:::

## Why it exists

`DramaboxClient` wraps Dramabox's monolithic `TTSServer`, which builds every
component in **one** dtype. On MPS no single dtype works:

- the Gemma prompt encoder returns all-NaN encodings in fp16 (MAC_TEST_REPORT.md
  F10), so it must run in bf16;
- the vocoder hits an `Input type (float) and bias type (c10::Half)` clash in
  fp16 (F9), so it must run in fp32.

The staged s4 pipeline fixed this with a per-component dtype map. This engine
assembles the same components through the staged helpers instead of
`TTSServer`, and uses the same Euler recipe, the same chunking policy, and the
same edge trim. What the Lab plays is therefore what a book render will sound
like.

Studio wiring (`scripts/voicebank_studio.py`, `TtsLoadWorker.run`): VibeVoice
gets `VibeVoiceClient`. Dramabox gets `StagedLabEngine(device=...)` when the
resolved device is `"mps"`, and `DramaboxClient(device=...)` otherwise.

## At a glance

| Name | Purpose |
|---|---|
| `StagedLabEngine(device="mps")` | Load the encoder, conditioner, DiT and decoder once. Encode the negative prompt once. |
| `generate(*, prompt, output_path, voice_ref=None, seed=None, cfg_scale=None, stg_scale=None, duration_multiplier=None)` | Render one prompt to a trimmed WAV. |
| `_chunk_texts(prompt, duration_multiplier)` | Chunking identical to `build_plan` (`staged.chunk_texts` with the same policy). |
| `_ref_latent(voice_ref)` | Ref denoise and VAE encode, cached per path for the session. |

## API

### `StagedLabEngine`

```python
class StagedLabEngine:
    def __init__(self, device: str = "mps") -> None
```

Loads every component warm and keeps it resident:

| Component | Built with | dtype on MPS | dtype elsewhere |
|---|---|---|---|
| Gemma `PromptEncoder` (`audio_only=True`) | `resolve_gemma_root(paths["gemma_root"], bnb_4bit)` | **bf16** | `_knobs` dtype (bf16) |
| `AudioConditioner` (VAE encoder) | `paths["audio_components"]` | fp16 | bf16 |
| Audio DiT | `staged._build_dit(paths, dev, dtype, compile_model)` | fp16, eager | bf16, `torch.compile` |
| `AudioDecoder` (vocoder) | `paths["audio_components"]` | **fp32** | fp32 by default; `_knobs` dtype (bf16) with `LNVOX_S4_DECODE_DTYPE=auto` |

The encoder quantization and the decoder dtype come from the same
[effective-recipe helpers](/library/tts/dramabox-client/#effective-recipe-helpers)
as the staged ctx and decode phases. The encoder is **unquantized** unless
`s4_effective_encoder_precision(device)` returns `"bnb4"`, which needs a
non-MPS device and `LNVOX_S4_ENCODER_PRECISION=bnb4`.

Other constructor behaviour:

- `params = PlanParams(**DramaboxClient.DEFAULT_PARAMS)`, then
  [`staged.apply_chunk_policy(params, device)`](/library/tts/staged/#apply_chunk_policy),
  the function `build_plan` uses. It honours `LNVOX_S4_CHUNK_CAP` (default
  22 s / 18 s with the word split), keeps 22 s on MPS when the knob is an
  opt-out (F11), and records whether the word-split fallback applies.
- The negative prompt (`DEFAULT_NEG`) is encoded once and **cast from the
  encoder dtype to the DiT dtype**. Mixing a bf16 negative context with an fp16
  positive one trips a hard Metal assertion (`MPSNDArrayMatrixMultiplication`
  needs matching accumulator dtypes). The staged path avoids this because it
  casts ctx tensors when it loads them.
- The RE-USE ref denoiser is built lazily on first use and shared across refs.

**Memory**: about 32 GB resident (23 GB bf16 encoder, 6 GB DiT, small VAEs),
which fits the 36 GB unified-memory budget in DESIGN.md §11.3. Measured on an
M4 Max: the warm load takes about 51 s, and a roughly 6 s audition renders in
about 39 s once warm (30 Euler steps at about 1.2 s per step).

#### `generate`

```python
def generate(
    self,
    *,
    prompt: str,
    output_path: Path,
    voice_ref: Path | None = None,
    seed: int | None = None,
    cfg_scale: float | None = None,
    stg_scale: float | None = None,
    duration_multiplier: float | None = None,
) -> None
```

Uses the same contract as `DramaboxClient.generate`, so the Studio calls either
engine the same way. Steps:

1. Copy `params`, overriding every argument that is not `None`.
2. Split `prompt` with `_chunk_texts`, which calls
   [`staged.chunk_texts`](/library/tts/staged/#chunk_texts). Prompts whose
   estimate exceeds the cap go through Dramabox's `chunk_prompt_for_duration`.
   While a cap is enforced (the default), every piece then also goes through
   `staged._force_split_oversize`.
3. Get the ref latent from `_ref_latent(voice_ref)` when `voice_ref` is given:
   decode 10 s, run the RE-USE denoise if available (on macOS it logs a warning
   and skips), prep, then VAE-encode.
4. For each chunk, under `torch.inference_mode()`:
   - encode the chunk text and cast it to the DiT dtype;
   - run `staged.denoise_chunk` with
     `n_frames = _n_frames(_estimate_duration(text, dm), fps)`, passing the
     negative context only when `cfg_scale > 1.0`;
   - decode in the decoder dtype;
   - equal-power crossfade onto the previous chunks (50 ms).
5. Write `output_path` with `staged._write_wav`, which is atomic, then
   `trim_file(output_path)`.

**Writes**: `output_path`. Nothing is written to `cache/tts/`, because Lab
renders are not cached.

## Notes

- **Ref latents are cached by path string** for the life of the engine. If you
  edit a clip file in place during a Studio session, the engine keeps using the
  old latent until you reload it. The staged s4 path hashes clip content
  instead.
- **Same recipe knobs as s4.** Chunking, encoder quantization and decode
  dtype follow the `LNVOX_S4_*` knobs through the functions the staged phases
  use, so the Lab chunks a prompt exactly as a staged render on the same
  device would. The knobs are read when the engine loads; reload the engine
  after changing them.
- The watermark is never applied here, which matches the s4 default
  (`watermark=False`).
- Only `device` values starting with `mps` get the MPS dtype map. The class
  would build with CUDA knobs on other devices, but nothing in the repo uses it
  that way. Note that the Studio picks this engine only for a device of
  exactly `"mps"`; any other device string goes to `DramaboxClient`, which
  refuses MPS devices.
