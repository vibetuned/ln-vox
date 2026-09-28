---
title: lnvox.tts.schema
description: Pydantic models for Stage 4 output — RenderedBeat, ChapterAudio and the per-chapter manifest.json every TTS path writes.
sidebar:
  order: 1
  label: schema
---

`lnvox.tts.schema` defines the contract between Stage 4 (TTS) and everything
downstream of it. All three render paths write it: the monolithic Dramabox path,
the staged Dramabox path and the VibeVoice session path. Stage 5 mixing,
scenario sync and `lnvox audio purge` read it. If you add a TTS engine, the only
thing it has to produce is WAVs plus one of these manifests per chapter.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| `RenderedBeat` | Pydantic model | One rendered audio unit: a beat (Dramabox) or a session (VibeVoice). |
| `ChapterAudio` | Pydantic model | Every rendered unit of one chapter, in playback order, plus total duration. Serialized as `manifest.json`. |

Source: `src/lnvox/tts/schema.py`.

## API

### `RenderedBeat`

```python
class RenderedBeat(BaseModel)
```

One rendered audio unit.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `beat_id` | `str` | required | Stable id. Dramabox: `<scene_id>_b<NNNN>`, e.g. `01_s1_b0023` (the beat's index inside its scene, zero-padded to 4). VibeVoice: the session id `<scene_id>_v<NNN>`. It is also the WAV's file stem. |
| `scene_id` | `str` | required | Scene the unit belongs to. Stage 5 uses scene changes to choose the inter-scene pause. |
| `type` | `str` | required | `"narration"` or `"dialogue"`. For a VibeVoice session it is `"dialogue"` if any beat in the session is dialogue, otherwise `"narration"`. |
| `speaker` | `str` | required | Character name (`"Narrator"` for narration). For a VibeVoice session it is the speakers joined with `" + "`, in order of first appearance (`"Narrator + Alice"`). |
| `wav_path` | `str` | required | Path of the WAV **relative to `artifacts/<book>/`**, e.g. `05_audio/01/01_s1_b0000.wav`. |
| `duration_seconds` | `float` | required | Measured from the WAV on disk **after** the edge-silence trim, rounded to 3 decimals. `0.0` if the file cannot be read. |
| `cache_key` | `str` | required | 16-hex-char content hash, the file stem in `cache/tts/<cache_key>.wav`. See [s4_tts cache key](/library/stages/s4-tts/#_content_hash). |
| `cached` | `bool` | `False` | Whether the audio came from the content cache instead of a fresh render. The exact meaning depends on the path (see Notes). |

### `ChapterAudio`

```python
class ChapterAudio(BaseModel)
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `chapter_id` | `str` | required | Chapter id. It matches the directory name `05_audio/<chapter_id>/`. |
| `beats` | `list[RenderedBeat]` | `[]` | Rendered units in playback order (scene order, then beat order). |
| `total_duration_seconds` | `float` | `0.0` | Sum of the per-beat durations, rounded to 3 decimals. |

## manifest.json

Every render path writes one file per chapter with
`ChapterAudio.model_dump_json(indent=2)`:

```text
artifacts/<book>/05_audio/<chapter_id>/manifest.json       # Dramabox (monolithic + staged)
artifacts/<book>/05_audio_v2/<chapter_id>/manifest.json    # VibeVoice sessions
```

Example (Dramabox, abbreviated):

```json
{
  "chapter_id": "01",
  "beats": [
    {
      "beat_id": "01_s1_b0000",
      "scene_id": "01_s1",
      "type": "narration",
      "speaker": "Narrator",
      "wav_path": "05_audio/01/01_s1_b0000.wav",
      "duration_seconds": 7.412,
      "cache_key": "3f9c2a1b0d4e5f67",
      "cached": false
    },
    {
      "beat_id": "01_s1_b0001",
      "scene_id": "01_s1",
      "type": "dialogue",
      "speaker": "Alice",
      "wav_path": "05_audio/01/01_s1_b0001.wav",
      "duration_seconds": 2.103,
      "cache_key": "a07b5e9c11d2e3f4",
      "cached": true
    }
  ],
  "total_duration_seconds": 9.515
}
```

Writers:

| Writer | Path | Written how |
|---|---|---|
| [`s4_tts.render_chapter`](/library/stages/s4-tts/#render_chapter) | monolithic Dramabox | `Path.write_text` once the chapter's last beat is done |
| [`staged._finalize`](/library/tts/staged/#_finalize) | staged Dramabox, decode phase | atomic (`manifest.json.tmp` then `os.replace`), for every chapter in the plan |
| [`s4_vibevoice.render_chapter`](/library/stages/s4-vibevoice/#render_chapter) | VibeVoice | `Path.write_text` once the chapter's last session is done |

Readers: `lnvox s5` (it iterates `05_audio[_v2]/*/` and **skips any directory
without a `manifest.json`**, which is how a leftover `_staged/` directory is
ignored), [s5_mix](/library/stages/s5-mix/),
[scenario_sync](/library/stages/scenario-sync/) and `lnvox audio purge`
(see [utilities](/stages/utilities/)). Stage 6 derives its beat timings from
`05_audio/` as well.

## Notes

- **Durations are post-trim.** Every path trims leading and trailing silence
  before it measures the WAV (see [trim](/library/tts/trim/)). Downstream
  timing in s5, s6 and scenario sync therefore follows the trimmed audio
  automatically.
- **`cached` differs by path.** Monolithic and VibeVoice set it when
  `cache/tts/<key>.wav` existed at the moment that beat or session was reached.
  Staged sets it from the plan (`PlanRender.cached`), meaning the cache WAV
  existed when `build_plan` ran. Renders produced earlier in the same staged
  run report `false`.
- **Manifests are rewritten wholesale.** A run with `--limit N` or
  `--chapters` writes manifests that contain only the beats it covered. A
  later full run rewrites them.
- The schema has no model or recipe field. The `cache_key` is the only link
  back to the render recipe (it hashes `MODEL_VERSION` plus the variant tag,
  see [render quality](/guides/render-quality/)).
