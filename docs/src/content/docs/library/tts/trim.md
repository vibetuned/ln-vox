---
title: lnvox.tts.trim
description: Deterministic edge-silence trim for rendered TTS WAVs - RMS envelope, -40 dBFS threshold, 100 ms keep-pad, idempotent, LNVOX_S4_NO_TRIM kill switch.
sidebar:
  order: 5
  label: trim
---

`lnvox.tts.trim` removes the leading and trailing silence that TTS engines
leave on a render (DESIGN.md §2.6). Dramabox sizes its latent from a duration
**estimate** (`duration_multiplier` 1.1), and the overshoot renders as silence,
mostly at the start. Measured on 2026-07-24 at -40 dBFS, a rendered play had a
median 2.2 s of leading silence, 70% of its lines had more than 1 s, and 29% of
all rendered audio was edge silence. A novel carried about 15%.

The trim is plain DSP: it works with any engine and is idempotent. It runs at
the moment a WAV lands in `05_audio/`, and it also upgrades the content cache
entry in place. Every duration written to a manifest is therefore measured on
trimmed audio, so s5 mixing, s6 sync and scenario sync follow it with no extra
work.

## At a glance

| Name | Value / signature | Purpose |
|---|---|---|
| `DEFAULT_THRESHOLD_DB` | `-40.0` | Absolute RMS threshold, dBFS. |
| `DEFAULT_PAD_MS` | `100.0` | Audio kept outside the first and last above-threshold sample. |
| `_WIN_MS` | `10.0` | RMS envelope window. |
| `_MIN_TRIM_SECONDS` | `0.05` | Below this much removable silence, the file is left untouched. |
| `trim_array(audio, sr, *, threshold_db, pad_ms)` | function | Trim a numpy array and return what was removed. |
| `trim_file(path, *, threshold_db, pad_ms)` | function | Trim a WAV in place and return the seconds removed. |

## API

### `trim_array`

```python
def trim_array(
    audio,
    sr: int,
    *,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    pad_ms: float = DEFAULT_PAD_MS,
)
```

Returns `(trimmed_audio, lead_removed_s, trail_removed_s)`.

1. Downmix to mono for detection only (`audio.mean(axis=1)` for `(n, ch)`
   input). The returned slice keeps every channel.
2. Build the RMS envelope: `sqrt(convolve(mono², ones(win)/win, mode="same"))`
   with `win = max(1, int(sr * 10 / 1000))`, computed in float64.
3. Find the samples where the envelope exceeds `10 ** (threshold_db / 20)`,
   which is 0.01 linear for -40 dBFS.
4. Keep `[first - pad, last + 1 + pad)`, clamped to the array bounds.

**Parameters**

- `audio`: float samples, shape `(n,)` or `(n, channels)`.
- `sr`: sample rate in Hz.
- `threshold_db`: detection threshold. Default `-40.0`.
- `pad_ms`: keep-pad on each side. Default `100.0`.

**Returns** the trimmed slice, which is a view into `audio`, along with the
seconds removed at each edge.

An input with **no** sample above the threshold is returned untouched, with
`(audio, 0.0, 0.0)`. An all-silent render is an upstream failure (see
MAC_TEST_REPORT.md F10), and it has to stay audible as one. Emptying the file
would hide it.

### `trim_file`

```python
def trim_file(
    path: Path,
    *,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    pad_ms: float = DEFAULT_PAD_MS,
) -> float
```

Reads `path` with soundfile, runs `trim_array`, and rewrites the file in place
**with the original PCM subtype** (for example `PCM_16`). Returns the total
seconds removed.

It is a no-op and returns `0.0` in two cases:

- `os.environ.get("LNVOX_S4_NO_TRIM") == "1"`. Only the exact string `"1"`
  counts.
- The total removable silence is under `_MIN_TRIM_SECONDS` (50 ms). This avoids
  rewriting nearly every WAV just to shave a few samples.

**Writes**: `path`, in place, as a non-atomic `sf.write`.

**Env vars**: `LNVOX_S4_NO_TRIM`. `lnvox s4 --no-trim` sets it to `1`.

## Where it runs

| Caller | When |
|---|---|
| [`s4_tts.render_chapter`](/library/stages/s4-tts/#render_chapter), monolithic | After every fresh render, before the WAV is copied into the cache. Also on every cache hit, so legacy untrimmed cache entries get upgraded. A real trim copies the result back to the cache. |
| [`staged._finalize`](/library/tts/staged/#_finalize), staged decode | On every placed beat. A real trim copies the result back to the cache. |
| [`VibeVoiceClient._save_normalized`](/library/tts/vibevoice-client/#_save_normalized) | Calls `trim_array` on the 24 kHz mono model output **before** resampling. Only the outer edges of a session are trimmed; pauses between turns inside it are kept. It checks `LNVOX_S4_NO_TRIM` itself. |
| [`StagedLabEngine.generate`](/library/tts/lab-engine/#generate) | After writing the audition WAV, so the Lab sounds like the book will. |

## Idempotency

After one trim, each edge holds at most `pad_ms` of sub-threshold audio before
the first above-threshold sample. A second pass therefore finds `first ≈ pad`
and `start = max(0, first - pad) ≈ 0`, removes less than 50 ms, and returns
`0.0` without touching the file. This is what makes re-trimming on every cache
hit and every `_finalize` pass safe. `tests/test_trim.py` checks it.

## Notes

- **`--no-trim` only affects new renders.** The cache key does not include the
  trim setting, and cached entries were already trimmed in place when they were
  first placed. To get untrimmed audio for beats that are already rendered, you
  have to evict their cache entries (for example with `lnvox audio purge`, see
  [utilities](/stages/utilities/)) and render again with `--no-trim`.
- It is the escape hatch for voices whose soft lead-in falls below -40 dBFS.
  Lowering `threshold_db` would need a code change; there is no env knob for it.
- Tests: `tests/test_trim.py` covers edge removal with the pad kept, stereo
  handling, the all-silent input, idempotency, PCM subtype preservation, the
  kill switch, and the under-50 ms no-op. It runs with plain `python`
  or under pytest.
