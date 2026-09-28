---
title: lnvox.tts.vibevoice_client
description: VibeVoiceClient - VibeVoice-Large wrapper for multi-speaker session renders, per-device dtype/attention, model resolution and 48 kHz stereo normalization.
sidebar:
  order: 6
  label: vibevoice_client
---

`lnvox.tts.vibevoice_client` wraps VibeVoice-Large, the second Stage 4 engine
(DESIGN.md §16). One `generate_session` call renders a whole multi-speaker
script: `Speaker N: ...` lines with up to 4 distinct voices. Dialogue
turn-taking and prosody therefore stay continuous within a session. Voice
identity comes **only** from reference WAVs (zero-shot cloning). Unlike
Dramabox, there is no descriptor-text channel.

Callers: `lnvox s4 --tts-backend vibevoice` (through
[s4_vibevoice.run](/library/stages/s4-vibevoice/#run), which does the session
planning) and the Voicebank Studio TTS Lab
([guide](/guides/voicebank-studio/)).

VibeVoice is a real package. `scripts/setup_vibevoice.sh` clones the community
fork (`vibevoice-community/VibeVoice`) to `external/VibeVoice/`, pinned to a
known commit that `LNVOX_VIBEVOICE_REF` can override. The launcher's
`prepare_vibevoice_env` then runs `uv pip install -e external/VibeVoice`.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| `VibeVoiceClient` | class | One VibeVoice-Large model resident for the whole stage. |
| `VibeVoiceClient.MODEL_VERSION` | constant | `"vibevoice-large-cfg1.3-ddpm10-48k"`, the cache-key component at the default 10 DDPM steps. |
| `VibeVoiceClient.model_version_for(ddpm_steps=None)` | classmethod | The cache-key component for a given step count, e.g. `"vibevoice-large-cfg1.3-ddpm20-48k"`. |
| `VibeVoiceClient.DEFAULT_PARAMS` | constant | `{"cfg_scale": 1.3, "ddpm_steps": 10, "seed": 42}` |
| `VibeVoiceClient.TARGET_SR` | constant | `48_000`, the output sample rate. |
| `_resolve_model_path(explicit)` | function | Explicit argument, then `LNVOX_VIBEVOICE_MODEL`, then `models/VibeVoice-Large`, then the HF id. |
| `_pick_attn_implementation(device)` | function | `flash_attention_2` on CUDA when importable, otherwise `sdpa`. |

## API

### `_resolve_model_path`

```python
def _resolve_model_path(explicit: str | None) -> str
```

The first match wins:

1. `explicit`, when it is truthy.
2. `$LNVOX_VIBEVOICE_MODEL`, which may be any local path or HF repo id.
3. `models/VibeVoice-Large`, relative to the working directory, if it contains
   a `config.json`. This is where `setup_vibevoice.sh` downloads the weights
   (about 18 GB).
4. `"microsoft/VibeVoice-Large"`.

### `_pick_attn_implementation`

```python
def _pick_attn_implementation(device: str) -> str
```

Returns `"flash_attention_2"` when `device` starts with `cuda` and
`import flash_attn` succeeds, and `"sdpa"` otherwise. flash-attn has no aarch64
wheel (DGX Spark), and it does not exist on MPS or CPU.

### `VibeVoiceClient`

```python
class VibeVoiceClient:
    DEFAULT_PARAMS = {"cfg_scale": 1.3, "ddpm_steps": 10, "seed": 42}
    MODEL_VERSION = "vibevoice-large-cfg1.3-ddpm10-48k"
    TARGET_SR = 48_000

    @classmethod
    def model_version_for(cls, ddpm_steps: int | None = None) -> str

    def __init__(
        self,
        device: str = "cuda",
        model_path: str | None = None,
        ddpm_steps: int | None = None,
    ) -> None
```

Loads `VibeVoiceProcessor.from_pretrained(model_path)` and
`VibeVoiceForConditionalGenerationInference.from_pretrained(model_path,
torch_dtype=..., attn_implementation=...)`, puts the model in eval mode, and
calls `set_ddpm_inference_steps(num_steps=ddpm_steps or 10)`.

**Parameters**

- `device`: torch device. `lnvox s4` passes the resolved `--device`.
- `model_path`: checkpoint, resolved with `_resolve_model_path`.
- `ddpm_steps`: diffusion-head steps. Defaults to 10.

**Raises** `RuntimeError` when the `vibevoice` package cannot be imported. The
message points to `scripts/setup_vibevoice.sh`.

**Attributes**: `device`, `model_path`, `processor`, `model`, `native_sr`
(read from `processor.audio_processor.sampling_rate`, falling back to 24000),
and `model_version`, the cache-key version this instance renders
(`model_version_for(ddpm_steps)`).

`model_version_for` is a classmethod so the lazy s4 path can compute keys
before the model loads. `None` (or 10) returns `MODEL_VERSION` unchanged, so
default renders keep their keys.

Per-device knobs (DESIGN.md §16.5):

| Device | dtype | Attention | Placement |
|---|---|---|---|
| `cuda*` | bf16 | `flash_attention_2` if importable, else `sdpa` | `device_map=device` |
| `mps` | fp32 (fp16 produces artifacts) | `sdpa` | `model.to(device)` |
| `cpu` | fp32 | `sdpa` | `model.to(device)` |

The weights are about 18 GB in bf16. fp32 on MPS roughly doubles that, so
DESIGN.md §16.5 calls MPS experimental and recommends 36 GB or more of unified
memory.

#### `generate_session`

```python
def generate_session(
    self,
    *,
    script: str,
    voice_refs: list[Path],
    output_path: Path,
    seed: int | None = None,
    cfg_scale: float | None = None,
) -> None
```

Renders `script` to `output_path` as a 48 kHz stereo WAV.

- `script` is `Speaker N: text` lines. Speakers are numbered `1..len(voice_refs)`
  in order of first appearance, and `voice_refs[i]` is Speaker `i+1`'s cloning
  reference. [`Session.script()`](/library/stages/s4-vibevoice/#session) builds
  exactly this format.
- `seed` defaults to 42 and is applied with `torch.manual_seed(seed)` on every
  call, so re-renders reproduce and a different seed gives a different take.
- `cfg_scale` defaults to 1.3.

It runs the processor on `text=[script]`, `voice_samples=[[...refs]]` with
padding and an attention mask, moves the tensors to the device, and calls
`model.generate(max_new_tokens=None, cfg_scale=...,
tokenizer=processor.tokenizer, generation_config={"do_sample": False})`. The
first speech output is then saved by `_save_normalized`.

#### `_save_normalized`

```python
def _save_normalized(self, audio, output_path: Path) -> None
```

Converts the model's mono output (24 kHz) to the format the rest of the
pipeline assumes, 48 kHz stereo (DESIGN.md §2.6 and §16.4). Stage 5's silence
pads and the ffmpeg concat demuxer need homogeneous inputs.

1. Convert the output to a float32 numpy array, squeezed. Anything other than
   1-D raises `RuntimeError`.
2. Unless `LNVOX_S4_NO_TRIM == "1"`, apply
   [`trim_array`](/library/tts/trim/#trim_array) at the native rate. Only the
   outer edges are trimmed; pauses inside the session are kept.
3. `soxr.resample(native_sr → 48000)` when the rates differ.
4. Duplicate to stereo and write with soundfile, creating parent directories.

The cache stores this normalized, trimmed WAV. Upsampling adds no quality; it
only makes the files uniform, so a book that mixes engines still concatenates
cleanly.

## Notes

- **Cache key.** `s4_vibevoice` keys sessions on
  `(script, "|".join(ref filenames), model_version)`. The DDPM step count is
  part of the version, so a client built with another `ddpm_steps` must be
  paired with `model_version_for(ddpm_steps)`. `s4_vibevoice.run` checks the
  constructed client's `model_version` against the one it keyed with and
  raises `RuntimeError` on a mismatch, before anything is rendered or cached.
  The CLI never passes `ddpm_steps`. `cfg1.3` is still fixed in the version:
  `generate_session(cfg_scale=...)` overrides are per call and not part of the
  key, and `s4_vibevoice` never passes one.
- **Transformers conflict.** The fork pins `transformers==4.51.3`, while
  Dramabox's Gemma encoder needs 4.52 or newer. One venv therefore serves one
  engine at a time. `prepare_vibevoice_env` installs the pin, and
  `prepare_tts_env` restores `transformers>=4.52` (DESIGN.md §16.6).
- **No staged mode.** VibeVoice is one checkpoint with a boot of about 30 to 60
  s. Crash recovery is the monolithic retry loop (`scripts/s4_retry.sh`), and
  `--staged` together with vibevoice is a hard error.
- **Quality notes** (DESIGN.md §16.7 and §16.9). Pronunciation and hallucination
  rate beat Dramabox, but pacing is weaker. Occasional filler words or speaker
  drift on long sessions can be fixed by re-rolling with a different seed or
  lowering `--max-session-chars`.
