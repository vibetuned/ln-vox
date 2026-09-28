---
title: lnvox.stages.s4_vibevoice
description: VibeVoice Stage 4 renderer - session planning (4 speakers, DEFAULT_MAX_SESSION_CHARS), script format, ref resolution, cache and the 05_audio_v2 layout.
sidebar:
  order: 7
  label: s4_vibevoice
---

`lnvox.stages.s4_vibevoice` is the v2 Stage 4 renderer (DESIGN.md §16). It does
not render one beat at a time. It groups consecutive beats of a scene into
**sessions** of at most 4 distinct speakers and at most `max_session_chars`
characters, and renders each session in **one**
[`VibeVoiceClient.generate_session`](/library/tts/vibevoice-client/#generate_session)
call. Output goes to `05_audio_v2/`, so Dramabox and VibeVoice renders of the
same book can coexist for A/B listening. The manifest reuses the
[`ChapterAudio`](/library/tts/schema/) schema with one entry per session, and
`lnvox s5 --v2` mixes it unchanged.

It is called by `lnvox s4 --tts-backend vibevoice` (or with
`LNVOX_TTS_BACKEND=vibevoice`). The planner is pure logic with no torch import,
so `tests/test_sessions.py` tests it without the model.

## At a glance

| Name | Value / signature | Purpose |
|---|---|---|
| `MAX_SPEAKERS_PER_SESSION` | `4` | VibeVoice-Large's limit on distinct voices per session. |
| `DEFAULT_MAX_SESSION_CHARS` | `3000` | Default session size cap: roughly 3 to 4 minutes of speech. |
| `Session` | dataclass | A consecutive run of beats inside one scene. |
| `plan_scene(scene, *, max_chars=3000)` | function | Greedily partition a scene's beats into sessions. |
| `plan_chapter(chapter, *, max_chars=3000)` | function | `plan_scene` over every scene, in order. |
| `_resolve_refs(speakers, speaker_to_clip)` | function | One mandatory cloning ref per speaker. |
| `render_chapter(...)` | function | Render or cache each session and write the manifest. |
| `run(...)` | function | Render every chapter into `05_audio_v2`. |

## Artifact layout

```text
artifacts/<book>/05_audio_v2/<chapter_id>/<session_id>.wav   # 48 kHz stereo, edge-trimmed
artifacts/<book>/05_audio_v2/<chapter_id>/manifest.json      # ChapterAudio, one entry per session
cache/tts/<cache_key>.wav                                     # shared pool with Dramabox
```

`session_id = f"{scene_id}_v{n:03d}"`, where `n` counts sessions within the
scene (`01_s1_v000`, `01_s1_v001`, ...). `05_audio/` is never touched.

## API

### `Session`

```python
@dataclass
class Session:
    session_id: str
    scene_id: str
    speakers: list[str]      # order of first appearance == Speaker 1..N
    beats: list[DirectedBeat]

    @property
    def char_count(self) -> int
    def script(self) -> str
```

- `char_count` is the sum of `len(beat.text)` over the session's beats.
- `script()` returns one `Speaker N: <text>` line per beat. `N` is the
  speaker's 1-based position in `speakers`. The text is `DirectedBeat.text`
  with every whitespace run collapsed to one space (`_collapse`), because a
  newline would start a line with no speaker. The screenplay `prompt` and
  `direction` are **not** used: VibeVoice has no descriptor channel and would
  read a direction prefix aloud.

```text
Speaker 1: It was the last day of summer vacation, and Kamijou Touma had…
Speaker 2: You've got to be kidding me.
Speaker 1: The girl on his balcony did not look like she was kidding.
```

### `plan_scene`

```python
def plan_scene(
    scene: DirectedScene, *, max_chars: int = DEFAULT_MAX_SESSION_CHARS
) -> list[Session]
```

Walks the scene's beats in order and accumulates them into a session. The
current session closes when the next beat would:

- introduce a **5th distinct speaker** (`MAX_SPEAKERS_PER_SESSION`), or
- push the accumulated `len(text)` past `max_chars`.

Beats are never split. A single beat longer than `max_chars` gets a session to
itself. Sessions never span scenes. Speaker numbering restarts in each session:
the speaker who forces a split becomes Speaker 1 of the new session.

### `plan_chapter`

```python
def plan_chapter(
    chapter: ChapterDirected, *, max_chars: int = DEFAULT_MAX_SESSION_CHARS
) -> list[Session]
```

Concatenates `plan_scene` over `chapter.scenes`.

### `_resolve_refs`

```python
def _resolve_refs(
    speakers: list[str], speaker_to_clip: dict[str, Path | None]
) -> list[Path]
```

Returns one ref per speaker, in Speaker 1..N order. Refs are mandatory because
VibeVoice has no descriptor fallback. For each speaker it uses the assigned
clip, falls back to the **Narrator's** clip, and raises `RuntimeError` if
neither exists. The error asks you to re-run `lnvox voice cast`
([voice cast](/stages/voice-cast/)). The chosen filenames go into the cache
key.

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
    max_session_chars: int = DEFAULT_MAX_SESSION_CHARS,
    progress: Callable[[str], None] = print,
) -> ChapterAudio
```

Plans the chapter. For each session it:

1. Builds the script and resolves the refs.
2. Computes `cache_key = _content_hash(script, "|".join(ref.name for ref in refs), model_version)`.
3. On a cache hit, copies `cache_dir/<key>.wav` to the session WAV.
4. On a miss, calls `client.generate_session(script=..., voice_refs=refs,
   output_path=wav_path)` and copies the result into the cache.
5. Appends a `RenderedBeat` with `beat_id` = session id, `speaker` = the
   speakers joined with `" + "`, `type` = `"dialogue"` if any beat is dialogue
   (else `"narration"`), and the post-trim duration.

It reports progress once per session and writes `manifest.json` at the end.
The client is resolved lazily on the first miss, so a fully cached chapter
never loads the model.

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
    max_session_chars: int = DEFAULT_MAX_SESSION_CHARS,
    on_chapter_done: Callable[[ChapterDirected, ChapterAudio], None] | None = None,
    progress: Callable[[str], None] = print,
    limit: int | None = None,
) -> list[ChapterAudio]
```

Has the same shape as [`s4_tts.run`](/library/stages/s4-tts/#run). It uses
`_build_speaker_to_clip_path` (so a wrong voicebank fails loudly) and
`slice_chapters(limit)`, and constructs the client lazily. The first load
reports `Loading VibeVoice-Large (first load downloads ~18GB of weights)…`.
`--limit` counts **beats**, not sessions.

Keys are computed from `model_version` before the model loads. When the client
is constructed, `run` compares its `model_version` attribute (if it has one)
with the `model_version` it was given and raises `RuntimeError` on a mismatch,
for example a client built with another `ddpm_steps`
([`model_version_for`](/library/tts/vibevoice-client/)). Otherwise such a
client would file its audio under, and later reuse, another recipe's keys.

The CLI passes `output_dir = artifacts/<book>/05_audio_v2`,
`cache_dir = cache/tts`, `model_version = VibeVoiceClient.MODEL_VERSION`,
`max_session_chars = --max-session-chars`, and
a `client_factory` that returns `VibeVoiceClient(device=device)`.

## Notes

- **Cache granularity.** Editing one line re-renders its whole session, up to
  3000 characters, rather than one beat of up to 375 characters. Lower
  `--max-session-chars` for finer caching or to relieve VRAM on 24 GB cards.
  Raise it for longer continuous prosody.
- **No re-trim on cache hits.** Unlike the Dramabox paths, a cache hit is only
  copied. The trim happens once, inside `VibeVoiceClient._save_normalized`,
  when the session is rendered.
- **Downstream limits.** Session WAVs have no per-beat timestamps. The launcher
  therefore skips Stage 6 and scenario sync for VibeVoice audio (DESIGN.md
  §16.2 and §16.10), and `lnvox audio purge` only scans `05_audio/`.
- **Tests.** `tests/test_sessions.py` covers the speaker cap, the character
  cap, the rule that an over-long beat is never split, scene boundaries, speaker
  numbering and whitespace collapse, Narrator fallback and the missing-ref
  error, the wrong-voicebank guard, and the render, cache and manifest round
  trip with a fake client. That test checks that a second render is served
  from the cache and that a different `model_version` re-renders. Two more
  check that the DDPM step count is part of the model version (the default key
  unchanged) and that `run` refuses a client whose recipe differs from the
  keys.
