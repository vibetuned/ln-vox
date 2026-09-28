---
title: lnvox.llm.schemas
description: Every pydantic model the LLM stages produce or consume, with field tables, the stage that writes each one and the artifact file it serialises to.
sidebar:
  order: 3
  label: schemas
---

`lnvox.llm.schemas` (`src/lnvox/llm/schemas.py`) defines the pydantic models that LLM output is validated against. They double as the on-disk format of most pipeline artifacts: s1's cast, s2's scenes, s3's directed beats, voice profiles and the scenario script are all `model_dump_json(indent=2)` of these classes. Casting and voicebank models (`VoiceTarget`, `MatchResult`, `BookCasting`, …) live in [lnvox.voices.schema](/library/voices/schema/).

## At a glance

| Model | Produced by | Serialised to | Consumed by |
| --- | --- | --- | --- |
| [`Character`](#character) / [`CharacterList`](#characterlist) | [s1](/stages/s1-characters/); [ingest-scenario](/stages/ingest-scenario/); `ingest --mode lecture` stub | `01_characters_per_chapter/<ch>.json`, `01_characters.json` | s2, s3, voice cast, scenario, Voicebank Studio, later volumes |
| [`CharacterMergeGroup`](#charactermergegroup) / [`CharacterMergeProposal`](#charactermergeproposal) | s1 merge LLM | not persisted | `s1_characters._apply_merge_groups` |
| [`Beat`](#beat), [`Scene`](#scene), [`ChapterScenes`](#chapterscenes) | [s2](/stages/s2-scenes/) | `02_scenes/<ch>.json` | s3, voice audit, Voicebank Studio |
| [`SceneBoundary`](#sceneboundary) / [`ChapterBoundaries`](#chapterboundaries) | s2 pass 2a | not persisted | s2 pass 2b |
| [`SceneBeats`](#scenebeats) | s2 pass 2b | not persisted (folded into `Scene`) | s2 |
| [`VoiceProfile`](#voiceprofile) / [`VoiceProfileList`](#voiceprofilelist) | [s3](/stages/s3-director/), [scenario](/stages/scenario/) | `03_voice_profiles.json` | s3 / scenario (cache), `voice cast` (legacy order, if present) |
| [`DirectionCue`](#directioncue) / [`SceneDirections`](#scenedirections) | s3 direction LLM | not persisted (folded into `DirectedBeat.direction`) | s3 |
| [`DirectedBeat`](#directedbeat), [`DirectedScene`](#directedscene), [`ChapterDirected`](#chapterdirected) | s3, [lecture](/stages/lecture/), scenario | `03_directed/<ch>.json` | s4 (both backends), s6, scenario-sync, voice audit |
| [`BlockClass`](#blockclass) | ingest-epub lecture classifier | not persisted | `ingest.blocks` |
| [`NormalizedBeat`](#normalizedbeat) / [`NormalizedBeats`](#normalizedbeats) | lecture normalize LLM | not persisted (folded into `DirectedBeat.text`) | lecture |
| [`ScriptItem`](#scriptitem), [`SceneStructure`](#scenestructure) | ingest-scenario structuring LLM | `cache/scenario/<key>.json` (LLM cache) | ingest-scenario |
| [`RosterEntry`](#rosterentry) / [`RosterList`](#rosterlist) | ingest-scenario roster LLM | `cache/scenario/<key>.json`; entries also in `00_script.json` | ingest-scenario |
| [`ScriptScene`](#scriptscene), [`ScenarioScript`](#scenarioscript) | ingest-scenario | `00_script.json` | scenario, scenario-sync |
| [`ScenarioCue`](#scenariocue) / [`ScenarioDirections`](#scenariodirections) | scenario direction LLM | not persisted (folded into `DirectedBeat.emotion` / `.direction`) | scenario |

Artifact paths are relative to `artifacts/<book_id>/` (see [Artifacts reference](/reference/artifacts/)).

## Shared types and validators

```python
Gender = Literal["male", "female", "nonbinary", "unknown"]
AgeBand = Literal["child", "teen", "young_adult", "adult", "elder", "unknown"]
BeatType = Literal["narration", "dialogue"]
BlockKind = Literal["prose", "code", "table", "figure", "footnote", "equation", "drop"]
Emotion = Literal["calm", "joy", "sadness", "anger", "fear", "surprise", "disgust"]
ScriptItemType = Literal["dialogue", "staging", "cue"]
```

### `_coerce_str_to_list`

```python
def _coerce_str_to_list(v)
```

A `mode="before"` validator. `None` becomes `[]`, a non-empty string `s` becomes `[s]`, an empty string becomes `[]`, and anything else passes through. Gemma sometimes returns `"aliases": "Sir Patrick"` where a list is expected. It's applied to `Character.aliases` / `.evidence`, `CharacterMergeGroup.names` and `SceneBoundary.cast`, and imported by [lnvox.voices.schema](/library/voices/schema/) for `VoiceTarget`'s keyword lists.

### `maxItems` caps

Several list fields carry `json_schema_extra={"maxItems": N}`. The cap goes into the JSON Schema sent as `guided_json`, so on vLLM a looping model is *forced by the grammar* to close the array. It is **not enforced during validation**: an over-long list from a backend that ignores `guided_json` still validates, and real results are never rejected for size.

| Field | `maxItems` |
| --- | --- |
| `Character.aliases` | 12 |
| `Character.evidence` | 6 |
| `CharacterList.characters` | 60 |
| `CharacterMergeProposal.merges` | 60 |
| `VoiceProfileList.profiles` | 120 |
| `SceneDirections.directions` | 300 |
| `NormalizedBeats.beats` | 200 |
| `SceneStructure.items` | 300 |
| `RosterList.entries` | 80 |
| `ScenarioDirections.directions` | 300 |

## Stage 1 — characters

### `Character`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | required | Canonical name, the form the text uses most often. It's the key for speakers, casting and profiles everywhere downstream. |
| `aliases` | `list[str]` | `[]` (max 12) | Other proper names / distinctive epithets. Coerced from a scalar. |
| `gender` | `Gender` | `"unknown"` | `male`, `female`, `nonbinary`, `unknown`. |
| `approx_age` | `AgeBand` | `"unknown"` | `child` … `elder`, or `unknown`. |
| `description` | `str` | required | 2–4 sentences: physical, voice, manner, affiliation, origin. Used by casting and s3. |
| `evidence` | `list[str]` | `[]` (max 6) | Short verbatim quotes justifying the description. Coerced from a scalar. |

### `CharacterList`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `characters` | `list[Character]` | required (max 60) | The cast. Validation also accepts the keys `cast_of_characters` and `cast` (`AliasChoices`; `populate_by_name=True`), because larger Gemma models sometimes rename the key. Always serialised as `characters`. |

Written by s1 per chapter to `01_characters_per_chapter/<chapter_id>.json` (cache) and merged to `01_characters.json`. `ingest-scenario` writes `01_characters.json` from the `scenario_characters` prompt. `lnvox ingest --mode lecture` writes the stub `{"characters": []}` if the file is missing. It's read by s2 (`name` + `aliases` as the prompt cast), s3, `voice cast`, the scenario stage, `scripts/voicebank_studio.py`, and later volumes of a series through [find_prior_volumes](/library/series/#find_prior_volumes).

### `CharacterMergeGroup`

One set of draft-cast names the s1 merge LLM judges to be the same person. The LLM returns only groups, never a rewritten cast. `s1_characters.merge_clusters` applies them to its own deterministic clusters.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `canonical` | `str` | required | Preferred display name. Should be one of `names`. |
| `names` | `list[str]` | `[]` | Two or more names from the draft list denoting one person. Coerced from a scalar. |

### `CharacterMergeProposal`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `merges` | `list[CharacterMergeGroup]` | `[]` (max 60) | Also accepted as `groups` or `merge_groups`. An empty list means "merge nothing". |

Not persisted. The merge provenance goes to `01_characters_merge_log.json`, which is a plain dict and not a schema from this module.

## Stage 2 — scenes and beats

### `Beat`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | `BeatType` | required | `narration` or `dialogue`. |
| `text` | `str` | required | Spoken words (dialogue: quotes and attribution tags stripped) or narration prose. This is what gets voiced. |
| `speaker` | `str \| None` | `None` | Canonical cast name for dialogue, or `"Unknown"` if unattributable. `null` for narration. s2 rewrites aliases to canonical names, and s3 canonicalises again. |
| `source_span` | `str` | `""` | Verbatim contiguous slice of the source (quotes, tags and whitespace kept). It's the exact match key s6 uses to find the beat in the EPUB. |

### `SceneBoundary`

s2 pass 2a output: one scene's extent before beats are tagged.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `scene_id` | `str` | required | `"<chapter_id>_s<N>"`. s2 renumbers scenes sequentially across chunks before pass 2b. |
| `location_hint` | `str` | `""` | Short where/when phrase. |
| `cast` | `list[str]` | `[]` | Canonical names present in the scene. Coerced from a scalar. |
| `start_paragraph` | `int` | required | 1-indexed, chapter-global first paragraph. The LLM answers in chunk-local numbers, which s2 clamps to the chunk and offsets by `Chunk.base_paragraph`. |
| `end_paragraph` | `int` | required | 1-indexed, chapter-global last paragraph (inclusive). |

### `ChapterBoundaries`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `chapter_id` | `str` | required | Echo of the chapter id. |
| `scenes` | `list[SceneBoundary]` | required | Should tile the chunk. An empty list is treated as "one scene = whole chunk". |

### `SceneBeats`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `beats` | `list[Beat]` | required | s2 pass 2b output for one scene. An empty list for non-empty text raises `RuntimeError` in s2 (a stalled-LLM guard). |

### `Scene`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `scene_id` | `str` | required | `"<chapter_id>_s<N>"`. |
| `location_hint` | `str` | `""` | From the boundary pass. |
| `start_paragraph` | `int` | `0` | Chapter-global range from pass 2a. `0` means unknown (legacy artifacts). |
| `end_paragraph` | `int` | `0` | As above. |
| `beats` | `list[Beat]` | required | Ordered beats. |

### `ChapterScenes`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `chapter_id` | `str` | required | Chapter id from `00_text.jsonl`. |
| `scenes` | `list[Scene]` | required | Scenes in order. |

Serialised to `02_scenes/<chapter_id>.json`. s2 treats a cached file with no scenes, or with any zero-beat scene, as broken and re-segments it.

## Stage 3 — director

### `VoiceProfile`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | required | Canonical character name, or `"Narrator"`. |
| `voice_descriptor` | `str` | required | 6–15 word Dramabox-ready descriptor (`"<age> <accent> <gender>, <timbre>, <manner>"`). |
| `accent` | `str` | `"any"` | Common Voice accent code (`us`, `england`, …) or `any`. |
| `gender` | `str` | `""` | `male` / `female`, snapped to the assigned clip after the LLM call. |
| `age_band` | `str` | `""` | `teen` / `young_adult` / `adult` / `elder`, snapped to the assigned clip. |
| `written_for_clip` | `str \| None` | `None` | The casting's `assigned_clip_id` when the profile was written (`""` if none). Stamped by code; hidden from the guided JSON schema (`SkipJsonSchema`), so the model never sees or fills it. `None` = a profile from before the record existed. |
| `written_for_descriptor` | `str \| None` | `None` | The casting's locked `voice_descriptor` when the profile was written (`""` if none). Same rules as `written_for_clip`. |

### `VoiceProfileList`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `profiles` | `list[VoiceProfile]` | required (max 120) | One per cast member plus the Narrator, in cast order. |

Serialised to `03_voice_profiles.json` by `s3_director.run` and `scenario.run`, through [`refresh_voice_profiles`](/library/stages/s3-director/#refresh_voice_profiles). It's a casting-aware cache: a profile is reused while its `written_for_*` record still matches the character's casting, and only new or recast characters are regenerated. `--regen-profiles` (on `lnvox s3` and `lnvox scenario`) regenerates every profile. `lnvox voice cast` also reads it when present (the legacy order, where s3 ran before voice cast) and uses its descriptors and accents to steer matching.

### `DirectionCue`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `line` | `int` | required | 1-indexed dialogue-line number from the prompt's numbered list. |
| `cue` | `str` | required | 2–8 word performance cue (emotion + vocal action). |

### `SceneDirections`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `directions` | `list[DirectionCue]` | required (max 300) | One cue per numbered line. Missing lines just get no cue. |

### `DirectedBeat`

The unit every TTS backend renders.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | `BeatType` | required | `narration` / `dialogue`. |
| `text` | `str` | required | Text to speak (lecture: speech-normalised; scenario: parenthetical acting cues stripped). |
| `speaker` | `str` | required | Always set. `"Narrator"` for narration and for scenario group lines. |
| `direction` | `str` | required | Voice descriptor plus cue (narration / lecture). In scenario mode it holds the human-readable cue in the script's language. |
| `prompt` | `str` | required | Final Dramabox string from `s3_director.format_prompt`: `(<direction>) "<text>"`, with the direction lowercased and its commas turned into `" - "`, e.g. `(adult male - gravelly bass - weary) "Go."`. Narration and lecture pass `direction`; scenario passes `"<descriptor>, <emotion>"`. The TTS cache key is built from it. |
| `source_span` | `str` | `""` | Carried from s2 (or the verbatim source slice) for sync. |
| `source_paragraph` | `int` | `-1` | Lecture: chapter-global paragraph index, used by s6 to anchor visual elements. Scenario: chapter-global dialogue-line index, used by scenario-sync to regroup split lines. `-1` in narration mode. |
| `emotion` | `str` | `"calm"` | Scenario mode: one of the 7 `Emotion` values. `"calm"` elsewhere. It's a plain `str`, so old artifacts still validate. |

### `DirectedScene`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `scene_id` | `str` | required | Narration: from s2. Lecture: `"<chapter_id>_s1"`. Scenario: `"<scene_id>_g<NN>"` per staging-delimited run. |
| `location_hint` | `str` | `""` | Narration: from s2. Scenario: the scene title. |
| `beats` | `list[DirectedBeat]` | required | Beats after same-speaker merging and sentence-boundary splitting, targeting at most 375 chars each (`MAX_MERGED_BEAT_CHARS`; a single longer sentence is kept whole). |

### `ChapterDirected`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `chapter_id` | `str` | required | Chapter id. In scenario mode, the script scene id (`"01"`, `"02"`, …). |
| `voice_descriptors` | `dict[str, str] \| None` | `None` | Speaker → voice descriptor the chapter's prompts were composed with. On a re-run, a mismatch with the current profiles re-voices the chapter. `None` = a file from before the record existed, treated as current and stamped on first load. |
| `normalized` | `bool \| None` | `None` | Lecture mode: whether the speech-normalize pass ran. A mismatch with the run's setting rebuilds the chapter. `None` elsewhere, and for lecture files from before the record existed. |
| `scenes` | `list[DirectedScene]` | required | Scenes in order. s5 pads between them. |

Serialised to `03_directed/<chapter_id>.json` by s3, `lnvox lecture` and `lnvox scenario`, and cached per chapter (a malformed file is regenerated; a stale `voice_descriptors` or `normalized` record re-voices or rebuilds it). This is the hand-off contract to [s4](/stages/s4-tts/), so every mode produces the same shape and s4/s5/s6 run unchanged.

## Lecture mode

### `BlockClass`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `kind` | `BlockKind` | required | `prose` (narrate), `drop` (excise), or a visual element: `code`, `table`, `figure`, `footnote`, `equation`. |
| `reason` | `str` | `""` | Short justification, kept for debugging. |

Only blocks the deterministic rules in [lnvox.ingest.blocks](/library/ingest/blocks/) can't classify reach the LLM (`max_tokens=256`). Not persisted.

### `NormalizedBeat`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `index` | `int` | required | 0-indexed position in the batch sent to the normalize pass. |
| `text` | `str` | required | Speakable respelling of that passage. |

### `NormalizedBeats`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `beats` | `list[NormalizedBeat]` | required (max 200) | Lecture sends batches of 25. A missing index, or a failed batch, keeps `text == source_span`. |

## Scenario mode

### `ScriptItem`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | `ScriptItemType` | required | `dialogue` (spoken), `staging` (stage direction), `cue` (sound / light / technical). |
| `speaker` | `str` | `""` | Speaker label as printed (markdown stripped). `""` unless dialogue. Canonicalised across the script after structuring. |
| `text` | `str` | required | Verbatim line text with markdown markers removed. Dialogue that isn't a whitespace-collapsed substring of its source chunk is demoted to `staging`. |

### `SceneStructure`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `items` | `list[ScriptItem]` | required (max 300) | LLM output for one scene chunk (long scenes are split into several parts). |

### `RosterEntry`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | required | Role name as printed. |
| `description` | `str` | `""` | Bio / notes as printed. |
| `actor` | `str` | `""` | Performer the script maps this role to, if any. |

### `RosterList`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `entries` | `list[RosterEntry]` | `[]` (max 80) | One entry per role in the script's own characters section. |

`SceneStructure`, `RosterList` and the scenario `CharacterList` are cached as compact JSON under `cache/scenario/<key>.json`. The key is the first 16 hex chars of `sha256(schema name, rendered prompt, model)`, so editing the script, a template or the model re-keys automatically.

### `ScriptScene`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `scene_id` | `str` | required | `"01"`, `"02"`, …; also the s4/s5 chapter id. |
| `title` | `str` | `""` | Scene header text. |
| `items` | `list[ScriptItem]` | `[]` | The scene's lines in order. |

### `ScenarioScript`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `scenario_id` | `str` | required | The book id passed to `ingest-scenario --id`. |
| `title` | `str` | `""` | Script title (defaults to the cleaned markdown file stem). |
| `language` | `str` | `""` | Language tag (`ingest-scenario --language`, CLI default `fr`). |
| `roster` | `list[RosterEntry]` | `[]` | Parsed characters section. |
| `scenes` | `list[ScriptScene]` | `[]` | All scenes. |

Serialised to `00_script.json`, the verbatim structured script read by `lnvox scenario` and `lnvox scenario-sync`.

### `ScenarioCue`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `line` | `int` | required | 1-indexed dialogue-line number within the scene. |
| `emotion` | `Emotion` | `"calm"` | One of the 7 emotions. It goes into the English TTS prompt as `"<descriptor>, <emotion>"`. |
| `cue` | `str` | `""` | 2–6 word cue in the script's language. Stored in `DirectedBeat.direction` and surfaced in the sync file. |

### `ScenarioDirections`

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `directions` | `list[ScenarioCue]` | required (max 300) | One per numbered dialogue line. An unusable answer leaves every line `calm` with no cue (a transport error fails the stage instead). |

## Notes

- **Schema changes are artifact-format changes.** Adding a required field breaks loading of every existing artifact of that type. New fields in this module are added with defaults (`source_paragraph=-1`, `emotion="calm"`, `Scene.start_paragraph=0`, `ChapterDirected.voice_descriptors=None`, `VoiceProfile.written_for_clip=None`) for that reason.
- **Validation is lenient by design.** Key aliases (`AliasChoices`), scalar-to-list coercion and non-enforced `maxItems` accept common Gemma deviations, so a near-miss doesn't cost a retry in [`LLMClient.structured`](/library/llm/client/#retry-semantics). `Literal` fields (`gender`, `approx_age`, `type`, `kind`, `emotion`) are still strict: an out-of-enum value fails validation and triggers a retry.
- The JSON shapes the model is asked for are spelled out in each [prompt template](/library/llm/prompts/). Keep templates and models in sync when editing either.
