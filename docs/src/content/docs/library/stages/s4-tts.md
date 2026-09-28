---
title: lnvox.stages.s4_tts
description: Monolithic Stage 4 Dramabox renderer - run(), render_chapter(), the content-hash cache key, speaker-to-clip mapping and manifest writing.
sidebar:
  order: 6
  label: s4_tts
---

`lnvox.stages.s4_tts` is the **monolithic** Dramabox renderer. It renders beat
by beat through one warm [`DramaboxClient`](/library/tts/dramabox-client/),
checks the content cache per beat, trims each WAV, and writes one manifest per
chapter. `lnvox s4` uses it when neither `--staged` nor
`--tts-backend vibevoice` is given. In the launcher that happens only when
`LNVOX_S4_STAGED=0`. The run goes through `scripts/s4_retry.sh`.

Its helpers (`_content_hash`, `_wav_duration`,
`_build_speaker_to_clip_path`, `slice_chapters`) are shared: the
[staged planner](/library/tts/staged/#build_plan) and the
[VibeVoice stage](/library/stages/s4-vibevoice/) import them, so every render
path keys the cache and maps voices the same way.

## At a glance

| Name | Purpose |
|---|---|
| `run(chapters, casting, voicebank, voicebank_root, output_dir, cache_dir, *, client_factory, model_version, ...)` | Render every chapter and return `list[ChapterAudio]`. |
| `render_chapter(chapter, *, client=None, client_provider=None, speaker_to_clip, output_dir, cache_dir, model_version, progress=print)` | Render one chapter and write its `manifest.json`. |
| `_content_hash(*parts)` | The TTS cache key. |
| `_build_speaker_to_clip_path(casting, voicebank, voicebank_root)` | Map character to absolute clip path, or `None`. Fails loudly on a wrong voicebank. |
| `slice_chapters(chapters, limit)` | Keep the first `limit` beats overall (smoke tests). |
| `_wav_duration(path)` | Frames divided by sample rate, via soundfile. `0.0` on error. |

## API

### `run`

```python
def run(
    chapters: list[ChapterDirected],
    casting: BookCasting,
    voicebank: Voicebank,
    voicebank_root: Path,
    output_dir: Path,
    cache_dir: Path,
    *,
    client_factory: Callable[[], "object"],
    model_version: str,
    on_chapter_done: Callable[[ChapterDirected, ChapterAudio], None] | None = None,
    progress: Callable[[str], None] = print,
    limit: int | None = None,
) -> list[ChapterAudio]
```

1. `output_dir.mkdir(parents=True, exist_ok=True)`.
2. Build `speaker_to_clip` with `_build_speaker_to_clip_path`. It raises before
   any rendering if the voicebank is wrong.
3. `chapters = slice_chapters(chapters, limit)`.
4. Wrap `client_factory` in a lazy holder. The model is constructed on the
   **first cache miss only**, and at that point `run` reports
   `Loading Dramabox (first model load downloads ~3-4GB of weights)…`. A fully
   cached re-run, for example one that only re-places trimmed WAVs, never
   touches the GPU.
5. Call `render_chapter` for each chapter, then `on_chapter_done(ch, result)`
   if it was given.

**Parameters**

- `chapters`: `ChapterDirected` list from `03_directed/*.json`
  ([schemas](/library/llm/schemas/)).
- `casting`: `BookCasting` from `04_voice_assignments.json`
  ([voices schema](/library/voices/schema/)).
- `voicebank`, `voicebank_root`: the loaded voicebank manifest and its
  directory (`$LNVOX_VOICEBANK`, default `voicebank`).
- `output_dir`: `artifacts/<book>/05_audio`.
- `cache_dir`: `cache/tts`.
- `client_factory`: zero-argument callable that returns an object with
  `generate(prompt=, output_path=, voice_ref=)`. The CLI passes a
  nested factory that returns `DramaboxClient(device=device)`.
- `model_version`: goes into every cache key. The CLI passes
  `DramaboxClient.MODEL_VERSION + s4_variant_tag(device, staged=False)`.
- `limit`: render only the first N beats overall.

The client is created lazily through a factory, so this module still imports
when the optional `tts` extra is not installed.

### `render_chapter`

```python
def render_chapter(
    chapter: ChapterDirected,
    *,
    client=None,
    client_provider: Callable[[], "object"] | None = None,
    speaker_to_clip: dict[str, Path | None],
    output_dir: Path,
    cache_dir: Path,
    model_version: str,
    progress: Callable[[str], None] = print,
) -> ChapterAudio
```

Pass either `client` or `client_provider`. `client_provider` is resolved on the
first cache miss. For each beat, in scene then beat order:

1. `beat_id = f"{scene.scene_id}_b{idx:04d}"`, where `idx` is the beat's index
   within its scene. The WAV path is `output_dir/<chapter_id>/<beat_id>.wav`.
2. `ref_token = clip.name` (the clip **filename**) or `"no-ref"`, then
   `cache_key = _content_hash(beat.prompt, ref_token, model_version)`.
3. **Cache hit** (`cache_dir/<key>.wav` exists): copy the cache entry to the
   WAV path, then `trim_file(wav_path)`. When that trim removes anything, which
   only happens for legacy entries made before the trim existed, copy the result
   back to upgrade the cache entry.
4. **Cache miss**: `client.generate(prompt=beat.prompt, output_path=wav_path,
   voice_ref=clip)`. If the WAV exists afterwards, trim it and copy it into the
   cache.
5. Append a `RenderedBeat`. `wav_path` is relative to `artifacts/<book>`, the
   duration is measured post-trim and rounded to 3 decimals, and `cached` is
   `True` on a hit.

Progress is reported every 5 beats, and once per chapter as
`✓ <chapter>: N beats, T s (R rendered, C cached)`. After the last beat,
`manifest.json` is written with `Path.write_text`, which is not atomic.

**Returns** the chapter's [`ChapterAudio`](/library/tts/schema/#chapteraudio).

**Writes**: `05_audio/<chapter>/<beat_id>.wav`, `05_audio/<chapter>/manifest.json`,
`cache/tts/<key>.wav`.

### `_content_hash`

```python
def _content_hash(*parts: str) -> str
```

The TTS content-cache key: SHA-256 over each part (UTF-8, errors ignored),
**each followed by a `\x1f` separator**, truncated to the first 16 hex chars.
Stage 4 always calls it with three parts:

| Part | Dramabox (monolithic and staged) | VibeVoice |
|---|---|---|
| 1 | `beat.prompt`, the full screenplay prompt including the direction | `session.script()` |
| 2 | ref clip **filename** (`clip.name`), or `"no-ref"` | `"\|".join(ref filenames)` |
| 3 | `MODEL_VERSION + variant tag` (the effective recipe on the device) | `VibeVoiceClient.model_version_for(ddpm_steps)`, i.e. `MODEL_VERSION` at the default 10 steps |

What this implies:

- Editing a beat's text **or** its direction re-renders only that beat.
- Recasting a character to a different clip re-keys every beat that character
  speaks.
- Editing a clip file **in place** under the same filename does **not** re-key
  anything. Use `lnvox audio purge --speaker <name>` to evict those beats
  ([utilities](/stages/utilities/)).
- Changing the render recipe re-keys the whole book
  ([render quality](/guides/render-quality/)).
- The Dramabox and VibeVoice engines share `cache/tts/` without collisions,
  because their `MODEL_VERSION`s differ.

### `_build_speaker_to_clip_path`

```python
def _build_speaker_to_clip_path(
    casting: BookCasting,
    voicebank: Voicebank,
    voicebank_root: Path,
) -> dict[str, Path | None]
```

Maps `character_name` to `(voicebank_root / clip.clip_path).resolve()`.

- A character with an empty `assigned_clip_id` maps to `None`. This is a valid
  state: the beat is rendered from its descriptor alone, and in the key it
  appears as `"no-ref"`.
- A character whose **assigned** clip id is missing from the loaded voicebank
  raises `RuntimeError`, listing up to 3 examples. That situation means the
  casting was made against a different bank. Carrying on would silently drop the
  cast voices and re-key the whole cache. The message suggests
  `export LNVOX_VOICEBANK=voicebank-fr`, for example.

### `slice_chapters`

```python
def slice_chapters(
    chapters: list[ChapterDirected], limit: int | None
) -> list[ChapterDirected]
```

Returns `chapters` unchanged when `limit is None`. Otherwise it keeps the first
`limit` beats overall, cutting scenes and chapters mid-way and dropping any
that end up empty. It works on `model_copy`s, so the inputs are not modified.
`staged._slice_to_limit` does the same thing for the staged path.

### `_wav_duration`

```python
def _wav_duration(path: Path) -> float
```

Returns `info.frames / info.samplerate` from `soundfile.info`. On **any**
exception it returns `0.0`. A beat whose render produced no file therefore
still appears in the manifest, with a duration of 0.0.

## Notes

- **Crash recovery is external.** Long Dramabox runs can SIGKILL after 10 to 15
  minutes of continuous denoise, probably from CUDA fragmentation on RTX
  50-series cards. `scripts/s4_retry.sh` restarts `lnvox s4`, and the per-beat
  cache turns each restart into a resume. Each restart pays the full four-model
  boot, which is why the [staged path](/library/tts/staged-driver/) became the
  default.
- **Recipe.** This path always renders with `TTSServer`'s built-in chunking
  (45 s / 37 s) and decode dtype. Only `LNVOX_S4_ENCODER_PRECISION` applies, and
  its default here is `bnb4`. `cli.stage4` refuses explicit
  `LNVOX_S4_CHUNK_CAP` / `LNVOX_S4_DECODE_DTYPE` values with exit 2 before it
  calls `run` ([Stage 4](/stages/s4-tts/#the-env-knob-refusal)). Empty values
  count as unset.
- **Not on MPS.** `cli.stage4` refuses this path on an MPS device (exit 2), and
  `DramaboxClient` itself raises there: its single-dtype `TTSServer` renders
  silence on Metal (MAC_TEST_REPORT.md F10). Use `--staged`.
- **Placement always refreshes.** A cache hit is always copied over the chapter
  WAV. The staged `_finalize` re-copies whenever the chapter copy is not an
  exact copy of the cache entry, so a staged run after a monolithic one
  replaces these files once.
