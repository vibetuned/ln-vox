---
title: lnvox.tts.dramabox_client
description: DramaboxClient wrapper around Dramabox's TTSServer, the Gemma checkpoint resolver, and the LNVOX_S4_* render-recipe knobs that shape the TTS cache key.
sidebar:
  order: 2
  label: dramabox_client
---

`lnvox.tts.dramabox_client` is the only module that knows where Dramabox lives
and how to import it. It holds three things:

- `DramaboxClient`, a thin wrapper around Dramabox's monolithic `TTSServer`
  used by the monolithic `lnvox s4` path and by the Voicebank Studio TTS Lab on
  CUDA and CPU. It refuses MPS.
- The import and patch helpers that the [staged](/library/tts/staged/) phases
  and the [lab engine](/library/tts/lab-engine/) also call.
- The render-recipe knob readers (`s4_encoder_precision`, `s4_chunk_cap`,
  `s4_decode_dtype`), the `s4_effective_*` helpers that apply the MPS
  constraints on top of them, and `s4_variant_tag`, which turns the effective
  recipe into a suffix on the TTS cache key.

Dramabox is not pip-installable. `scripts/setup_dramabox.sh` clones it to
`external/DramaBox/`, and this module injects it into `sys.path`.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| `_ensure_path()` | function | Put `external/DramaBox/src` and `external/DramaBox` on `sys.path`, or raise if the clone is missing. |
| `_patch_torchaudio_save()` | function | Replace `torchaudio.save` with a soundfile shim so torchcodec is not needed. Idempotent. |
| `resolve_gemma_root(default_root, use_bnb_4bit)` | function | Choose the Gemma encoder checkpoint that matches the bnb setting. |
| `s4_encoder_precision(staged=True)` | function | Read `LNVOX_S4_ENCODER_PRECISION` and return `"bf16"` or `"bnb4"`. |
| `s4_chunk_cap()` | function | Read `LNVOX_S4_CHUNK_CAP` and return the cap in seconds, or `None`. |
| `s4_decode_dtype()` | function | Read `LNVOX_S4_DECODE_DTYPE` and return `"fp32"` or `None`. |
| `s4_effective_encoder_precision(device, staged=True)`, `s4_effective_chunk_cap(device)`, `s4_effective_decode_dtype(device)` | functions | What the render on `device` actually uses. MPS forces bf16, a 22 s cap and fp32. |
| `s4_variant_tag(device, staged=True)` | function | Cache-key suffix for the effective recipe, such as `+enc-bf16+cap22+dec-fp32`. |
| `DramaboxClient` | class | One `TTSServer` held in VRAM. `generate()` renders a prompt to a WAV. |
| `DramaboxClient.MODEL_VERSION` | constant | `"dramabox-dit-v1-cfg2.5-stg1.5-dm1.1"`, the base of every Dramabox cache key. |
| `DramaboxClient.DEFAULT_PARAMS` | constant | Sampling defaults passed to every render. |

## API

### `_ensure_path`

```python
def _ensure_path() -> None
```

Checks that `<repo>/external/DramaBox` exists, then prepends
`external/DramaBox/src` and `external/DramaBox` to `sys.path` if they are not
already present. Dramabox's `src` package uses bare imports
(`from model_downloader import ...`), so the inner `src/` directory must be
importable as top-level. The repo root is located as three parents up from
this file, so the check does not depend on the working directory.

**Raises** `RuntimeError` when `external/DramaBox` is missing. The message
tells you to run `scripts/setup_dramabox.sh`.

The staged phases call `staged._ensure_ltx_path()`, which calls this and also
adds `external/DramaBox/ltx2`.

### `_patch_torchaudio_save`

```python
def _patch_torchaudio_save() -> None
```

Since torchaudio 2.9, `torchaudio.save()` routes through torchcodec, which
needs the `torchcodec` package and a matching ffmpeg ABI. vLLM wants torchaudio
2.10 or newer, while Dramabox's `requirements.txt` pins 2.8. To avoid
depending on either version, this replaces `torchaudio.save` with a shim that
writes the WAV with `soundfile`. The shim transposes `(channels, frames)` to
`(frames, channels)` when `channels_first=True`, which is the torchaudio
default. It marks the module with `torchaudio._lnvox_save_patched = True`, so a
second call does nothing.

### `resolve_gemma_root`

```python
def resolve_gemma_root(default_root: str, use_bnb_4bit: bool) -> str
```

Returns the Gemma-3-12B prompt-encoder checkpoint directory to hand to
Dramabox.

- `use_bnb_4bit=True`: returns `default_root` unchanged. This is Dramabox's
  own download, `unsloth/gemma-3-12b-it-bnb-4bit`.
- `use_bnb_4bit=False`: returns
  `huggingface_hub.snapshot_download("unsloth/gemma-3-12b-it")`
  (`_GEMMA_UNQUANTIZED_REPO`). This is the ungated unquantized mirror, about
  23 to 24 GB, downloaded once into the standard HF cache.

The unquantized path exists because Dramabox's downloader only fetches the
**pre-quantized** checkpoint. Its packed 4-bit weights are `[N, 1]` uint8 blobs
that only bitsandbytes (CUDA-only) can expand. Loading them with
`bnb_4bit=False` fails with per-layer size-mismatch errors
(MAC_TEST_REPORT.md F8). Callers: `DramaboxClient.__init__`,
`staged._phase_ctx`, `StagedLabEngine.__init__`.

The next three functions are the render-recipe knobs. They read the environment on every call, so each staged-phase
subprocess sees the value it inherited from the parent `lnvox s4`. Values are
stripped and lower-cased before they are compared, so an empty or blank value
is the same as unset. `lnvox s4` calls all three before any work and turns a
`ValueError` into exit 2. The
[render quality guide](/guides/render-quality/) covers when to change them.

### `s4_encoder_precision`

```python
def s4_encoder_precision(staged: bool = True) -> str
```

| `LNVOX_S4_ENCODER_PRECISION` | Returns |
|---|---|
| unset or empty | `"bf16"` if `staged` else `"bnb4"` |
| `bf16` | `"bf16"`: unquantized Gemma encoder |
| `bnb4` | `"bnb4"`: Dramabox's pre-quantized 4-bit encoder |
| anything else | raises `ValueError` |

The two paths have different defaults. The staged ctx phase loads the encoder
with the GPU to itself, so the bf16 encoder fits. The monolithic
`TTSServer` holds the encoder, DiT and vocoder in one process, where bf16 does
not fit on a 32 GB card.

### `s4_chunk_cap`

```python
def s4_chunk_cap() -> float | None
```

| `LNVOX_S4_CHUNK_CAP` | Returns |
|---|---|
| unset or empty | `22.0` |
| `0`, `none`, `off` | `None`: no override, so the chunker's 45 s applies (MPS still gets 22 s, see below) |
| a positive number | `float(value)` seconds |
| anything else (non-numeric, negative) | raises `ValueError` |

Only the staged planner applies it, through `s4_effective_chunk_cap`, as
`max_chunk_duration = cap` and `target_chunk_duration = max(6.0, cap - 4.0)`
(see [apply_chunk_policy](/library/tts/staged/#apply_chunk_policy)).

### `s4_decode_dtype`

```python
def s4_decode_dtype() -> str | None
```

| `LNVOX_S4_DECODE_DTYPE` | Returns |
|---|---|
| unset or empty | `"fp32"` |
| `fp32` | `"fp32"` |
| `auto` | `None`: the vocoder uses the per-device dtype from `staged._knobs()` |
| anything else | raises `ValueError` |

Only the staged decode phase reads it, through `s4_effective_decode_dtype`.

### Effective-recipe helpers

```python
def s4_effective_encoder_precision(device: str, staged: bool = True) -> str
def s4_effective_chunk_cap(device: str) -> float | None
def s4_effective_decode_dtype(device: str) -> str | None
```

What a staged render on `device` actually uses. Each one calls its knob reader
first, so a bad value raises on every device. On CUDA and CPU they return the
knob value unchanged. On MPS (`device.startswith("mps")`) the three reverts
cannot take effect:

| Helper | MPS result | Why |
|---|---|---|
| `s4_effective_encoder_precision` | always `"bf16"` (staged) | bitsandbytes is CUDA-only (F8), and fp16 Gemma returns all-NaN (F10). |
| `s4_effective_chunk_cap` | `22.0` when the knob is an opt-out; an explicit number is returned as is | The DiT mask crash on beats of about 25 s or more (F11). |
| `s4_effective_decode_dtype` | always `"fp32"` | The fp16 vocoder hits a float/half conv clash (F9). |

`staged._phase_ctx`, `_phase_decode`, `apply_chunk_policy` and
[StagedLabEngine](/library/tts/lab-engine/) all read these helpers, and so does
`s4_variant_tag`. The key therefore always describes what was rendered.

### `s4_variant_tag`

```python
def s4_variant_tag(device: str, staged: bool = True) -> str
```

Builds the cache-key suffix for the **effective** recipe on `device`. The
suffix records deviations from the **original v1 recipe** (bnb-4bit encoder,
45 s chunker, per-device decode dtype), not from the current defaults. Parts
are appended in this order:

1. `enc-<precision>` when `s4_effective_encoder_precision(device, staged) != "bnb4"`.
2. Only when `staged=True`: `cap<cap:g>` when `s4_effective_chunk_cap(device)`
   is not `None` (22.0 gives `cap22`, 30.5 gives `cap30.5`).
3. Only when `staged=True`: `dec-<dtype>` when `s4_effective_decode_dtype(device)`
   is truthy.

It returns `"+" + "+".join(parts)`, or `""` when there are no parts.

| Situation | CUDA / CPU tag | MPS tag |
|---|---|---|
| Staged, all defaults | `+enc-bf16+cap22+dec-fp32` | `+enc-bf16+cap22+dec-fp32` |
| Staged, `ENCODER_PRECISION=bnb4` | `+cap22+dec-fp32` | `+enc-bf16+cap22+dec-fp32` |
| Staged, `CHUNK_CAP=none` | `+enc-bf16+dec-fp32` | `+enc-bf16+cap22+dec-fp32` |
| Staged, `DECODE_DTYPE=auto` | `+enc-bf16+cap22` | `+enc-bf16+cap22+dec-fp32` |
| Staged, all three reverts | `""`, the v1 keys | `+enc-bf16+cap22+dec-fp32` |
| Staged, `CHUNK_CAP=30` | `+enc-bf16+cap30+dec-fp32` | `+enc-bf16+cap30+dec-fp32` |
| Monolithic, defaults | `""` | refused |
| Monolithic, `ENCODER_PRECISION=bf16` | `+enc-bf16` | refused |

Before 2026-09, the tag followed the raw knobs on every device, so the MPS
column matched the CUDA one. Mac renders made with a revert knob sit under
those old keys and are no longer looked up. Default keys did not change on
either platform.

Both render paths build their model version the same way,
`DramaboxClient.MODEL_VERSION + s4_variant_tag(device, ...)`. Staged uses
`s4_variant_tag(device)` inside [`build_plan`](/library/tts/staged/#build_plan).
Monolithic uses `s4_variant_tag(device, staged=False)` in `cli.stage4`. The
result feeds the [content hash](/library/stages/s4-tts/#_content_hash).

Because the tag measures distance from v1, defaults could change on
2026-08-04 without any `MODEL_VERSION` bump. Renders made back when these
settings were opt-in flags carry the same `+enc-bf16+cap22+dec-fp32` keys, so
they stayed valid.

### `DramaboxClient`

```python
class DramaboxClient:
    DEFAULT_PARAMS = {
        "cfg_scale": 2.5,
        "stg_scale": 1.5,
        "duration_multiplier": 1.1,
        "seed": 42,
        "denoise_ref": True,
        "watermark": False,
    }
    MODEL_VERSION = "dramabox-dit-v1-cfg2.5-stg1.5-dm1.1"

    def __init__(self, device: str = "cuda", **kwargs: Any) -> None
```

Builds one Dramabox `TTSServer` and keeps it resident for the lifetime of the
object. `TTSServer` loads all four models (Gemma encoder, LTX audio DiT, VAE
encoder, VAE decoder + BigVGAN BWE) at once. `DEFAULT_PARAMS` is also the source
of [`PlanParams`](/library/tts/staged/#planparams) on the staged path, so both
paths sample with the same settings.

Constructor steps:

1. On MPS (`device.startswith("mps")`), raise `RuntimeError` before any import
   or download (see below).
2. `_ensure_path()` and `_patch_torchaudio_save()`.
3. Import `model_downloader.get_all_paths` and `inference_server.TTSServer`
   from Dramabox. On first use, `get_all_paths()` downloads
   `ResembleAI/Dramabox` (DiT and audio components) and
   `unsloth/gemma-3-12b-it-bnb-4bit`. Dramabox fetches `nvidia/RE-USE` lazily
   later.
4. If `s4_encoder_precision(staged=False) == "bf16"`, which only happens when
   the env var sets it explicitly, set `bnb_4bit` to `False` by default
   (`kwargs.setdefault`, so an explicit kwarg is kept).
5. Resolve the Gemma root with `resolve_gemma_root(paths["gemma_root"],
   kwargs.get("bnb_4bit", True))`. A missing key means `TTSServer`'s own
   default, the quantized path.
6. `TTSServer(checkpoint=paths["transformer"],
   full_checkpoint=paths["audio_components"], gemma_root=..., device=device,
   **kwargs)`.

**Parameters**

- `device`: torch device string. `lnvox s4` passes the resolved `--device`.
- `**kwargs`: passed through to `TTSServer`, whose knobs are `dtype`
  (`"fp16"` or `"bf16"`), `compile_model` and `bnb_4bit`. With none passed,
  CUDA and CPU get `TTSServer`'s defaults (bf16, bnb-4bit, `torch.compile`).

**Attributes**: `device`, and `server` (the `TTSServer` instance).

**Raises** `RuntimeError` on MPS. `TTSServer` builds every component in
**one** dtype, and on MPS no single dtype works: the Gemma encoder returns
all-NaN encodings in fp16, which renders silent audio with exit code 0 (F10),
and the vocoder needs fp32 (F9). The message points to `lnvox s4 --staged`
(the launcher default) for books and to
[`StagedLabEngine`](/library/tts/lab-engine/) for single prompts, which is what
the Studio uses there. `lnvox s4` without `--staged` on MPS refuses earlier,
with exit 2.

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

Renders `prompt` (a screenplay-format beat prompt, `(<direction>) "<text>"`) to
`output_path`. Arguments that are not `None` override the matching
`DEFAULT_PARAMS` entry. `denoise_ref=True` and `watermark=False` are always
passed. Parent directories are created first. The call goes to
`TTSServer.generate_to_file(prompt=..., output=str(output_path),
voice_ref=str(voice_ref) or None, **params)`.

`voice_ref` is the voicebank clip whose timbre is cloned. `None` renders from
the prompt's descriptor text alone.

Long prompts are chunked inside Dramabox. When the duration estimate exceeds
`TTSServer`'s own `max_chunk_duration` default (45 s, target 37 s),
`generate_to_file` hands off to `generate_long`. `DramaboxClient` never
overrides these values, which is why `LNVOX_S4_CHUNK_CAP` cannot apply to the
monolithic path.

**Writes**: `output_path` (WAV). The caller, `s4_tts.render_chapter`, then
trims it and copies it into the cache.

## Notes

- **Cache keys.** `MODEL_VERSION` encodes the sampling recipe
  (`cfg2.5`, `stg1.5`, `dm1.1`). If you change `DEFAULT_PARAMS`, change
  `MODEL_VERSION` too, or the content cache will serve audio made with the old
  settings. Precision, chunking and decode changes go through
  `s4_variant_tag`, not `MODEL_VERSION`.
- **Dramabox stays unpatched** (DESIGN.md §11.3). Every device workaround
  lives in wrapper kwargs, a different checkpoint, or staged-phase composition.
- **Memory.** The monolithic process holds all four models. The bnb-4bit
  encoder is about 8 GB (DESIGN.md §15.1). The bf16 encoder is about 23 GB,
  which is why `LNVOX_S4_ENCODER_PRECISION=bf16` on the monolithic path risks
  OOM on a 32 GB card.
