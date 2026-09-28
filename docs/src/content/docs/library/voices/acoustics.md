---
title: lnvox.voices.acoustics
description: Per-clip acoustic metrics (recording quality plus median f0) cached in <voicebank>/acoustics.json for the casting audit.
sidebar:
  order: 5
  label: acoustics
---

`src/lnvox/voices/acoustics.py` measures what the manifest cannot say: whether two reference clips actually sound different, and how clean each one is. `lnvox voice audit` calls [`load_acoustics`](#load_acoustics) for the whole bank and hands the index to [lnvox.voices.audit](/library/voices/audit/), which uses `pitch` to detect collisions and `bandwidth` / `snr` / `hiss` to choose replacements (DESIGN.md §20.3).

## At a glance

| Name | Purpose |
|---|---|
| `ACOUSTICS_FILE` | `"acoustics.json"`, stored in the voicebank root |
| `_F0_MIN, _F0_MAX` | `60.0`, `400.0` Hz: the pitch search range |
| [`semitones_apart`](#semitones_apart) | Pitch distance in semitones |
| [`clip_acoustics`](#clip_acoustics) | Metrics for one clip file |
| [`load_acoustics`](#load_acoustics) | Cached index for a list of clips, building what is missing |

## API

### `semitones_apart`

```python
def semitones_apart(a: float, b: float) -> float
```

`abs(12 * log2(a / b))`. Returns `0.0` when either value is falsy or not positive, so an unmeasured pitch never produces a fake gap. Callers check for missing pitch themselves before treating `0.0` as "identical". For reference, 2.5 semitones (the audit's default threshold) is a frequency ratio of about 1.155, i.e. 100 Hz vs 115.5 Hz.

```python
semitones_apart(100, 200)  # 12.0
semitones_apart(0, 100)    # 0.0 (unknown, not identical)
```

### `clip_acoustics`

```python
def clip_acoustics(path: Path) -> dict | None
```

Recording quality plus median f0 for one clip.

1. Runs [`probe_recording_quality`](/library/voices/common-voice/#probe_recording_quality) on the file. If that returns `None` (unreadable, too short, almost no non-silent frames), so does this.
2. Loads the file mono at its native rate and runs `librosa.yin(y, fmin=60, fmax=400, sr=sr, frame_length=2048, hop_length=512)`.
3. Keeps frame estimates strictly inside (60, 400) Hz and takes their median as `pitch`. If 10 or fewer frames survive, or YIN raises, `pitch` is `0.0`.

**Returns** `{"bandwidth", "snr", "rumble", "hiss", "clip", "pitch"}`, with the first five exactly as defined by the quality probe.

The range is wider than any real speaker so an odd clip is still measured rather than dropped. YIN has no voicing decision: every frame gets an estimate, and the median over the whole clip is what "pitch" means here. The quality metrics are computed on the full concatenated reference clip; the probe's −90 dB silence floor keeps the seeder's zero-filled gaps out of the SNR.

### `load_acoustics`

```python
def load_acoustics(
    voicebank_dir: Path,
    clips: list[VoiceClip],
    *,
    progress=None,
) -> dict[str, dict]
```

Returns the acoustic index `{clip_id: metrics}` for `clips`, measuring and caching whatever is not already indexed.

1. Read `<voicebank_dir>/acoustics.json` if it exists; an unreadable or invalid file is treated as empty.
2. `missing` = clips whose `id` is not a key in the index.
3. If anything is missing: measure each with `clip_acoustics(voicebank_dir / clip.clip_path)`, add the successful ones, and rewrite the whole file (`json.dumps(index, indent=1, sort_keys=True)`).

**Parameters**

- `voicebank_dir` — the voicebank root; clip paths are resolved against it.
- `clips` — the clips to cover (the CLI passes the whole bank).
- `progress` — optional callable; gets `Indexing N clip(s) for acoustic audit…` and `  indexed i/N` every 50 clips.

**Returns** the full index, which may also contain entries for clips no longer in the manifest.

**Reads / Writes** `<voicebank_dir>/acoustics.json`:

```json
{
 "cv_051e865815e5": {
  "bandwidth": 10957.03125,
  "clip": 0.0001,
  "hiss": 0.08,
  "pitch": 196.4,
  "rumble": 0.004,
  "snr": 41.2
 }
}
```

(Values illustrative.) Requires the `voice` extra (`librosa`, `soundfile`); without it the first probe raises `RuntimeError`.

## Notes

- **Incremental.** A bank seeded before the audit existed is indexed on the first `lnvox voice audit`, which costs one quality probe plus one YIN pass per clip, so that first run is noticeably slower. Later audits only measure new clip ids.
- **Keyed by id only.** Nothing records a file hash or mtime. If you rebuild clips under the same ids (re-seeding with a different `--target-seconds`, or editing a WAV in place), the cached metrics go stale. Delete `acoustics.json`; it is a cache, not a source of truth, and deleting it costs exactly one rebuild.
- **Failures are retried.** Clips whose measurement returns `None` are never written, so they count as missing on every run: each audit re-probes them and rewrites the file.
- Per-language banks each get their own `acoustics.json`, because it lives next to that bank's `manifest.json`.
