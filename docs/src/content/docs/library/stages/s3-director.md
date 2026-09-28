---
title: lnvox.stages.s3_director
description: Stage 3 Director. Clip-anchored voice profiles, speaker canonicalisation, same-speaker merge, the 375-char split and Dramabox prompt composition.
sidebar:
  order: 3
  label: s3_director
---

Stage 3 turns the scenes from s2 into **directed beats**: one Dramabox-ready `prompt` per beat, built from a voice descriptor and a performance cue. It runs after [voice cast](/stages/voice-cast/), so each descriptor can match the reference clip the speaker was actually cast on. `lnvox s3` ([stage page](/stages/s3-director/)) calls `run()`. [Lecture mode](/library/stages/lecture/) and [scenario mode](/library/stages/scenario/) import `format_prompt`, `_split_long_text`, `refresh_voice_profiles` and the narrator constants from here, so this module is the single source of the prompt format and the beat-length policy.

## At a glance

| Symbol | Kind | Purpose |
| --- | --- | --- |
| `run` | function | Stage entry point: canonicalise speakers, refresh profiles, direct each chapter (cached, casting-aware) |
| `refresh_voice_profiles` | function | Load `03_voice_profiles.json`, regenerating only the profiles whose casting changed |
| `generate_voice_profiles` | function | One descriptor per character, kept from the cache, locked from the casting, or written by the LLM and snapped to the clip |
| `direct_chapter` / `direct_scene` | functions | Merge beats, one cue call per scene, compose prompts |
| `chapter_voice_descriptors` | function | Speaker → descriptor for a chapter: the record a cached chapter is checked against |
| `revoice_chapter` | function | Rebuild a cached chapter's prompts for changed descriptors, keeping its cues (no LLM) |
| `format_prompt` | function | `(<direction, lowercased, commas → " - ">) "<text>"` |
| `_merge_same_speaker` | function | Fuse adjacent same-speaker beats up to the cap, then split anything over it |
| `_split_long_text` | function | Sentence-boundary split to at most `max_chars` |
| `build_speaker_index` / `canonical_speaker` / `canonicalize_scene_speakers` | functions | Fold s2 speaker tags onto cast names |
| `MAX_MERGED_BEAT_CHARS` | constant | `375` characters, about 30 s of audio |
| `NARRATOR_NAME` | constant | `"Narrator"` |
| `DEFAULT_NARRATOR_DESCRIPTOR` | constant | `"adult, British, female, warm clear voice, measured and engaging storyteller"` |
| `DEFAULT_NARRATOR_GENDER` / `_AGE` / `_ACCENT` | constants | `"female"`, `"adult"`, `"england"` |

Schemas (`Beat`, `Scene`, `ChapterScenes`, `VoiceProfile`, `VoiceProfileList`, `SceneDirections`, `DirectedBeat`, `DirectedScene`, `ChapterDirected`) are in [lnvox.llm.schemas](/library/llm/schemas/). `BookCasting` and `Voicebank` are in [lnvox.voices.schema](/library/voices/schema/).

## API

### `run`

```python
def run(
    chapters_scenes: list[ChapterScenes],
    cast: CharacterList,
    client: LLMClient,
    output_dir: Path,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    regen_profiles: bool = False,
    on_chapter_done=None,
) -> list[ChapterDirected]
```

1. **Canonicalise speakers** in place with `canonicalize_scene_speakers(chapters_scenes, cast)`. If anything changed, it prints `[s3] canonicalised N beat speaker(s): <up to 5 "old → new" pairs>`. The rewrite applies to the in-memory scenes only. `02_scenes/` is not modified.
2. **Voice profiles.** `refresh_voice_profiles(...)` loads `03_voice_profiles.json` and regenerates only the profiles whose casting changed (every profile with `regen_profiles=True`, which is `lnvox s3 --regen-profiles`).
3. **Direct each chapter.** It loads `03_directed/<chapter_id>.json` if present and parseable, then compares the file's `voice_descriptors` record with `chapter_voice_descriptors(chapter, current profiles)`:
   - **Same**: the file is reused as-is. No LLM call, no write.
   - **A descriptor changed** (a recast, or `--regen-profiles`): `revoice_chapter` rebuilds the affected beats' `direction` and `prompt` with the new descriptor and keeps their cues. No LLM call. It prints `[s3] <chapter>: voice descriptor changed for <names> — re-voiced prompts (cues kept)`.
   - **Can't re-voice** (the chapter's speaker set changed, or a beat's `direction` doesn't start with the recorded descriptor): the chapter is re-directed through the LLM.
   - **No record** (`voice_descriptors` is `null`, a file from before the record existed): treated as current, so an upgrade doesn't re-pay the LLM for the whole book. It is stamped with the descriptors of the profiles file as it was on disk *before* this run's refresh (the file the latest chapters were written with), so a recast since then is still re-voiced.

   A missing or unparseable file is directed with `direct_chapter`. `on_chapter_done(chapter_scenes, ChapterDirected)` follows each chapter.

**Writes**: `03_voice_profiles.json` and `03_directed/<chapter_id>.json`, each only when its content changes.

:::note[A recast after s3 is free, but s4 re-renders it]
Re-voicing changes the `prompt` of the recast character's beats, and s4's content cache is keyed on the prompt and the reference clip. So those beats re-render, which they must anyway for the new clip. Everyone else's beats keep their prompts and their cached audio. `--regen-profiles` rewrites every descriptor, so it re-voices every chapter and s4 re-renders the whole book. Don't pass it on routine runs.
:::

### `generate_voice_profiles`

```python
def generate_voice_profiles(
    client: LLMClient,
    cast: CharacterList,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    keep: dict[str, VoiceProfile] | None = None,
) -> VoiceProfileList
```

Writes one `VoiceProfile` per cast member, plus the `Narrator` if the cast doesn't have one, using the stub description `"Neutral audiobook narrator for a modern action/fantasy young-adult novel."`. Each character's clip is found through `casting.castings[].assigned_clip_id` → `voicebank.clips[].id`.

For each character there are three paths:

- **Kept (no LLM).** The character is in `keep`, a cached profile `refresh_voice_profiles` found still current. It is reused verbatim.
- **Locked (no LLM).** The character's `CharacterCasting.voice_descriptor` is non-empty and its clip resolves in the voicebank. The descriptor is used verbatim, with `accent`, `gender` and `age_band` copied from the clip. Voice cast fills `voice_descriptor` for the Narrator (from `--narrator-clip`, auto-cast, or prior-volume reuse) and for any prior-volume reuse that carried one.
- **LLM-written.** Everyone else is sent in a single call, `voice_profiles.jinja`. Each entry is `{name, description, assigned_clip: {id, gender, age_band, accent} | null}`. On a partial refresh only the recast characters are sent, and the prompt only mentions the Narrator when it is among them. The prompt asks for `"<age band> <accent adjective> <gender>, <pitch/timbre>, <habitual manner>"` consistent with the clip. The budget is `budget_for(desired=max(4096, 400 * n + 1024), floor=4096)`, with `_salvage_voice_profiles` as the truncation salvage. If every character is locked, the LLM is not called.

Post-processing (the "snap"):

- A descriptor that is empty or one of `n/a`, `na`, `unknown`, `none`, `tbd` (case-insensitive) is replaced with `descriptor_from_clip(clip)`, or `DEFAULT_NARRATOR_DESCRIPTOR` when there is no clip. That applies even to non-narrator characters.
- With a clip, `accent`, `gender` and `age_band` are **overwritten** with the clip's values, whatever the LLM wrote. The descriptor text itself is not checked, so a descriptor that says "male" over a female clip survives. Check the output file if a voice sounds wrong.

Output order: cast order, then `Narrator`, then any extra names (invented by the LLM, or kept from the cache). A kept profile wins over a same-named one the LLM invents. As a last resort, if no Narrator profile exists, a stub with `DEFAULT_NARRATOR_DESCRIPTOR` is appended.

Every returned profile is stamped with what it was written for: `written_for_clip` (the casting's `assigned_clip_id`) and `written_for_descriptor` (its locked `voice_descriptor`), each `""` when unset.

Without a casting or voicebank (both `None`, or an empty voicebank because `$LNVOX_VOICEBANK/manifest.json` is missing), nothing is locked and nothing is snapped. The LLM writes unanchored descriptors.

### `refresh_voice_profiles`

```python
def refresh_voice_profiles(
    client: LLMClient,
    cast: CharacterList,
    profiles_path: Path,
    *,
    casting: BookCasting | None = None,
    voicebank: Voicebank | None = None,
    regen: bool = False,
) -> tuple[VoiceProfileList, VoiceProfileList | None]
```

The casting-aware profile cache, shared by s3 and [scenario mode](/library/stages/scenario/). It loads `profiles_path` (an unparseable file counts as absent) and keeps each cached profile that `_profile_is_current` accepts:

- **Recorded profile**: current while `(written_for_clip, written_for_descriptor)` still equals the casting's `(assigned_clip_id, voice_descriptor)`.
- **Legacy profile** (no record; the launcher used to regenerate profiles on every run): current unless it provably describes a different voice. Generation snaps `gender`/`age_band`/`accent` onto the assigned clip and copies a locked descriptor verbatim, so a mismatch with the *current* clip's metadata, or with the current locked descriptor, means the character was recast.

Then `generate_voice_profiles(..., keep=kept)` fills in the rest, so only new or recast characters reach the LLM. When nothing changed, no LLM call is made. With `regen=True` nothing is kept. It prints `[profiles] regenerating N voice profile(s) whose casting changed or is new: <names>` (or `[profiles] generating voice profiles for N character(s)` on a first or forced run), writes the file only if its content changed, and returns `(profiles, previous)`. `previous` is the file as it was on disk, or `None`; `run` uses it to stamp chapters that have no record.

### `_salvage_voice_profiles`

```python
def _salvage_voice_profiles(raw: str) -> VoiceProfileList | None
```

Same approach as s1's salvage. It walks the `"profiles": [` array, decodes objects until the first incomplete one, and keeps those that validate. Characters missing from a partial list get **no** profile. In `direct_scene` their dialogue gets the descriptor `"voice unknown"`. The next `refresh_voice_profiles` asks for them again (a cast member without a cached profile is never "current"), and `run` re-voices their chapters.

### `direct_chapter`

```python
def direct_chapter(
    client: LLMClient, chapter_scenes: ChapterScenes, profiles: VoiceProfileList
) -> ChapterDirected
```

Calls `direct_scene` for each scene and wraps the results with the same `chapter_id`. It records `voice_descriptors = chapter_voice_descriptors(chapter_scenes, profiles)`.

### `chapter_voice_descriptors`

```python
def chapter_voice_descriptors(
    chapter_scenes: ChapterScenes, profile_map: dict[str, str]
) -> dict[str, str]
```

`{speaker: descriptor}` for every voice in the chapter, sorted by speaker: `"Narrator"` for narration beats, and the dialogue speaker (or `"Unknown"`) otherwise. The descriptor is the one `direct_scene` composes with: the speaker's profile, `DEFAULT_NARRATOR_DESCRIPTOR` for a Narrator without one, `"voice unknown"` for anyone else without one.

### `revoice_chapter`

```python
def revoice_chapter(
    chapter: ChapterDirected, current: dict[str, str]
) -> ChapterDirected | None
```

Re-composes a cached chapter for changed descriptors without an LLM call. The cue prompt never sees a descriptor, so the cues stay valid across a recast. For each beat whose speaker's descriptor changed, it strips the recorded descriptor off `direction` (an exact match means no cue; `"<old>, <cue>"` gives the cue), then rebuilds `direction` as `"<new>, <cue>"` (or `<new>`) and `prompt` with `format_prompt`. Other beats are untouched. It returns `None` when the recorded and current speaker sets differ, or when a `direction` doesn't carry the recorded descriptor. `run` then re-directs the chapter through the LLM.

### `direct_scene`

```python
def direct_scene(
    client: LLMClient, scene: Scene, profiles: VoiceProfileList
) -> DirectedScene
```

1. `merged = _merge_same_speaker(scene.beats)`.
2. `_build_scene_context(merged)` builds the scene in order, with narration as `(narration) <text>` and dialogue as `[L<n>] [<speaker>] "<text>"`, plus a numbered list `n. [<speaker>] "<text>"` of the dialogue lines only.
3. **One LLM call per scene**, and only if the scene has at least one dialogue line. It renders `scene_directions.jinja` and asks for `SceneDirections` (`[{line, cue}]`, 2–8-word cues). The budget is `budget_for(desired=max(2048, 40 * lines + 512), floor=2048)`. A bad or unparseable answer means no cues: every line falls back to the descriptor alone. A transport error (`is_transport_error`: the server is unreachable or returned a 5xx) is re-raised, so the step fails and the launcher's `ensure_vllm` retry can restart the server.
4. Composes each beat:

| Beat | `speaker` | `direction` |
| --- | --- | --- |
| narration | `"Narrator"` | Narrator profile descriptor, or `DEFAULT_NARRATOR_DESCRIPTOR`. No cue. |
| dialogue | `beat.speaker`, or `"Unknown"` | `"<descriptor>, <cue>"`, or just `<descriptor>` if there is no cue. The descriptor is the speaker's profile, or `"voice unknown"` |

   `prompt = format_prompt(direction, text)`. `source_span` is carried through. `source_paragraph` stays `-1` and `emotion` stays `"calm"` (the schema defaults).

The narration descriptor comes from the profile, so narration from every chapter shares one voice description. Cues exist only for dialogue.

### `format_prompt`

```python
def format_prompt(direction: str, text: str) -> str
```

Builds the Dramabox input string. Dramabox reads the direction aloud unless it is phrased a particular way, which was found empirically in the Voicebank Studio TTS Lab. The direction is **lowercased**, every comma (with surrounding whitespace) becomes `" - "`, and the result is wrapped in parentheses. The spoken text keeps its own casing and punctuation, inside double quotes.

```python
format_prompt("adult British male, gravelly bass, measured and weary, cold, clipped", "You're late.")
# '(adult british male - gravelly bass - measured and weary - cold - clipped) "You\'re late."'
```

:::note
The TTS cache key is built from `prompt`, so changing this format re-renders every beat.
:::

### `_merge_same_speaker`

```python
def _merge_same_speaker(beats: list[Beat]) -> list[Beat]
```

s2 often emits one beat per sentence. Fusing adjacent beats that share a voice gives Dramabox fewer, longer generations. The rules for each incoming beat, compared with the last beat in the output:

- It merges only if the types match **and** `len(prev.text) + 1 + len(beat.text) <= MAX_MERGED_BEAT_CHARS` (375).
- Narration and narration always merge, since the Narrator voices both.
- Dialogue and dialogue merge only if the speaker is set, is not `"Unknown"`, and equals the previous speaker. Two consecutive unattributed lines may belong to different people, so they are never fused.
- On a merge, `text` values are joined with a space, and non-empty `source_span` values are joined with a space. Adjacent beats are contiguous in the source, so the joined span stays a near-contiguous slice the sync stage can match.

**Final split pass.** Any beat still over 375 characters is split with `_split_long_text`. That covers a long beat that came straight from s2, which is common for long narration paragraphs. `source_span` is split the same way, and chunks are paired by index. If the span splits into fewer pieces than the text, the unpaired text chunks get `source_span: ""` and fall back to text matching in [s6](/library/stages/s6-sync/). The pass is mandatory: Dramabox OOMs on very long inputs (above roughly 3000 characters it asks for more than 24 GB of VRAM).

### `_split_long_text`

```python
def _split_long_text(text: str, max_chars: int = MAX_MERGED_BEAT_CHARS) -> list[str]
```

It strips `text` and returns `[text]` if that is at most `max_chars`. Otherwise it splits on the regex `(?<=[.!?])\s+`, which is whitespace immediately after `.`, `!` or `?` (the punctuation stays with the preceding sentence). It then packs sentences greedily, joined with single spaces, flushing before a sentence that would push the chunk over `max_chars`. It never splits inside a sentence: a single sentence longer than `max_chars` is emitted whole.

:::tip[Why a beat is still over 375 characters]
The boundary requires the terminal punctuation to be the character right before the whitespace. `…he said." Then` has `"` there, so it is **not** a boundary, and neither are `;`, `:`, `—` or `…`. A long paragraph of quoted speech, or one run-on sentence, comes out as one oversized chunk.
:::

### `build_speaker_index`

```python
def build_speaker_index(cast: CharacterList) -> dict[str, str]
```

Maps every spelling of a character to its canonical `name`. Keys come from `_speaker_key`: lowercase, whitespace collapsed, and one leading `the`/`a`/`an` stripped. Both the name and every alias are indexed. When an alias and a canonical name produce the same key, the canonical name wins.

### `canonical_speaker`

```python
def canonical_speaker(name: str | None, index: dict[str, str]) -> str | None
```

Resolves one s2 speaker tag:

- `None` or `""` (narration) is returned unchanged;
- anything that fully matches `(?:the\s+)?narrator` (case-insensitive) becomes `"Narrator"`;
- otherwise it returns `index[_speaker_key(name)]` if present, **else the original tag unchanged**. It never guesses. An unknown tag is kept and rendered with the `"voice unknown"` descriptor and no reference clip.

This matters because s4 looks up a speaker's reference clip by exact cast name. An s2 tag like `The Square`, for a cast entry `A SQUARE`, would otherwise render without the character's cast voice.

### `canonicalize_scene_speakers`

```python
def canonicalize_scene_speakers(
    chapters_scenes: list[ChapterScenes], cast: CharacterList
) -> tuple[int, dict[str, str]]
```

Applies `canonical_speaker` to every beat, in place. Returns `(changed_beat_count, {original_tag: canonical})`. It is idempotent: a second call changes 0 beats.

## Artifacts

`03_voice_profiles.json`:

```json
{
  "profiles": [
    {
      "name": "Lord Vex",
      "voice_descriptor": "adult British male, gravelly bass, measured and weary",
      "accent": "england",
      "gender": "male",
      "age_band": "adult",
      "written_for_clip": "cv_7f3a91",
      "written_for_descriptor": ""
    },
    {
      "name": "Narrator",
      "voice_descriptor": "adult, British, female, warm clear voice, measured and engaging storyteller",
      "accent": "england",
      "gender": "female",
      "age_band": "adult",
      "written_for_clip": "cv_2b8e04",
      "written_for_descriptor": "adult, British, female, warm clear voice, measured and engaging storyteller"
    }
  ]
}
```

`03_directed/<chapter_id>.json`: a `ChapterDirected`, the format s4 consumes.

```json
{
  "chapter_id": "03",
  "voice_descriptors": {
    "Lord Vex": "adult British male, gravelly bass, measured and weary",
    "Narrator": "adult, British, female, warm clear voice, measured and engaging storyteller"
  },
  "normalized": null,
  "scenes": [
    {
      "scene_id": "03_s1",
      "location_hint": "Vex's study at dusk",
      "beats": [
        {
          "type": "narration",
          "text": "The duke turned from the window.",
          "speaker": "Narrator",
          "direction": "adult, British, female, warm clear voice, measured and engaging storyteller",
          "prompt": "(adult - british - female - warm clear voice - measured and engaging storyteller) \"The duke turned from the window.\"",
          "source_span": "The duke turned from the window.",
          "source_paragraph": -1,
          "emotion": "calm"
        },
        {
          "type": "dialogue",
          "text": "You're late.",
          "speaker": "Lord Vex",
          "direction": "adult British male, gravelly bass, measured and weary, cold, clipped",
          "prompt": "(adult british male - gravelly bass - measured and weary - cold - clipped) \"You're late.\"",
          "source_span": "“You're late,” Vex said without turning.",
          "source_paragraph": -1,
          "emotion": "calm"
        }
      ]
    }
  ]
}
```

## Notes

- **Beat ids** are assigned later, by s4 (`<scene_id>_b<NNNN>`), counting beats in each directed scene. Because s3 merges and splits beats, s4 beat ids do not line up with s2 beat indices.
- **Silent degradations.** A dead server fails the step (profile generation and cue calls both re-raise transport errors). A cue answer that is bad or unparseable is still swallowed, and that scene's dialogue is cached with descriptor-only directions. If a chapter's dialogue has no cues, delete its `03_directed/` file and re-run.
- **`"voice unknown"`.** This descriptor appears when a dialogue speaker has no profile: a tag s3 couldn't resolve, or a character missing from a truncated profile list. Grep `03_directed/` for it.
- Design notes: DESIGN.md §2.5 (Director), §2.6 (the 375-char policy).
