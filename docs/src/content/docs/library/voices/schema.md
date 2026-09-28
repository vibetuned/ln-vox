---
title: lnvox.voices.schema
description: Pydantic models for the voicebank manifest and per-book voice casting, with the manifest.json and 04_voice_assignments.json formats.
sidebar:
  order: 1
  label: schema
---

`src/lnvox/voices/schema.py` defines the two JSON contracts of the casting subsystem: the voicebank index (`<voicebank>/manifest.json`, a `Voicebank`) and a book's casting (`artifacts/<book_id>/04_voice_assignments.json`, a `BookCasting`). You touch it when you add a voice source, read or hand-edit a casting, or write a tool that consumes either file (Stage 3, Stage 4, the Voicebank Studio and the casting audit all do).

## At a glance

| Name | Kind | Persisted in |
|---|---|---|
| `VBGender` | `Literal["male", "female"]` | both files |
| `VBAgeBand` | `Literal["teen", "young_adult", "adult", "elder"]` | both files |
| [`VoiceClip`](#voiceclip) | model | `manifest.json` → `clips[]` |
| [`Voicebank`](#voicebank) | model | `manifest.json` (top level) |
| [`VoiceTarget`](#voicetarget) | model | `04_voice_assignments.json` → `castings[].target` |
| [`_RankedChoice`](#_rankedchoice) | model (internal name, but persisted) | `castings[].ranked[]` |
| [`MatchResult`](#matchresult) | model | LLM response only, never written |
| [`CharacterCasting`](#charactercasting) | model | `castings[]` |
| [`BookCasting`](#bookcasting) | model | `04_voice_assignments.json` (top level) |

## API

### `VBGender` / `VBAgeBand`

```python
VBGender = Literal["male", "female"]
VBAgeBand = Literal["teen", "young_adult", "adult", "elder"]
```

The voicebank's demographic vocabulary. It is deliberately narrower than Stage 1's `Character.gender` (`male | female | nonbinary | unknown`) and `Character.approx_age` (`child | teen | young_adult | adult | elder | unknown`), see [lnvox.llm.schemas](/library/llm/schemas/). Common Voice has no child age band and the seeder keeps only the two binary gender codes; the `Literal` types reject anything else in the manifest. The matcher maps a character into this vocabulary through the target-inference LLM call, or skips the character when its gender is not `male`/`female` (see [cast_book](/library/voices/matcher/#cast_book)).

### `VoiceClip`

One voicebank entry: a mono reference WAV plus the metadata the matcher filters on.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | required | Unique clip id, prefixed by source. The Common Voice seeder writes `cv_` + the first 12 characters of the speaker's `client_id`. Castings reference this. |
| `source` | `str` | required | Dataset source. Documented values: `common_voice`, `artie`, `fair_speech`, `speech_accent_archive`, `manual`, `youtube`. Not validated; only `common_voice` and `manual` are produced today (seeder, Studio). |
| `clip_path` | `str` | required | WAV path relative to the voicebank root, e.g. `clips/cv_051e865815e5.wav`. |
| `duration_seconds` | `float` | required | Length of the written WAV, including the 0.25 s gaps the seeder inserts between utterances. |
| `gender` | `VBGender` | required | Hard filter key for the matcher and the audit's replacement search. |
| `age_band` | `VBAgeBand` | required | Hard filter key for the matcher; the audit may move to an adjacent band. |
| `accent` | `str` | `"any"` | Normalised accent code (`us`, `england`, `indian`, …, `other`, `any`). Free text in the schema; see [accent normalisation](/library/voices/common-voice/#_normalize_accent). |
| `sample_sentences` | `list[str]` | `[]` | Up to 3 transcripts of the source utterances. The first one (truncated to 160 chars) is shown to the ranking LLM. |
| `license` | `str` | `"CC0"` | |
| `notes` | `str` | `""` | Free-text provenance, e.g. `Common Voice (validated.tsv); speaker 051e8658…`. |

### `Voicebank`

Top-level manifest.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `version` | `str` | `"1"` | Schema version marker. Nothing reads it yet. |
| `clips` | `list[VoiceClip]` | `[]` | Every clip in the bank. Order matters in two places: the matcher's candidate list follows it, and the audit breaks quality ties by it. |

Load and save it through [lnvox.voices.manifest](/library/voices/manifest/).

### `VoiceTarget`

The search metadata inferred for one character (LLM call 1 of the matcher) and stored with the casting.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `gender` | `VBGender` | required | Hard filter: candidates must match exactly. |
| `age_band` | `VBAgeBand` | required | Hard filter: candidates must match exactly. |
| `accent_keywords` | `list[str]` | `[]` | Accent codes. Soft preference only: matching clips are moved to the front of the candidate list. `["any"]` (or empty) means no preference. |
| `timbre_keywords` | `list[str]` | `[]` | Descriptors such as `bright`, `deep`, `baritone`. Passed to the ranking LLM, never used as a filter. |
| `manner_keywords` | `list[str]` | `[]` | Delivery descriptors such as `deadpan`, `measured`. Passed to the ranking LLM only. |

A `mode="before"` validator (`_coerce_str_to_list` from `lnvox.llm.schemas`) on the three keyword lists turns a bare string into a one-element list and `None`/`""` into `[]`, because models occasionally emit a scalar where the schema wants a list.

### `_RankedChoice`

One ranked candidate returned by the ranking LLM call. The leading underscore marks it as internal, but it is persisted in every casting's `ranked` list.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `clip_id` | `str` | required | One of the candidate ids shown to the LLM. `cast_book` drops any other id before writing ([`_offered_choices`](/library/voices/matcher/#_offered_choices)). |
| `score` | `float` | required | 0..1 confidence claimed by the LLM. Only the list order is used; the score is informational. `0.0` marks a [fallback](/library/voices/matcher/#_fallback_choice) pick. |
| `reason` | `str` | required | One short justification sentence. |

### `MatchResult`

```python
class MatchResult(BaseModel):
    ranked_choices: list[_RankedChoice]
```

The structured-output schema for the ranking call (`voice_match.jinja`). Choices whose id was not among the candidates offered are dropped; the first remaining one is the committed pick, and the remaining list is copied into `CharacterCasting.ranked`.

### `CharacterCasting`

The casting of one character (or the Narrator).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `character_name` | `str` | required | Canonical name from `01_characters.json`, or exactly `Narrator`. This is the join key used by s3, s4 and the audit (against beat `speaker` values). |
| `target` | `VoiceTarget` | required | The target the clip was chosen against. For reused castings it is copied from the reused entry (prior volume, or this volume's own); for the Narrator auto-cast it is the fixed `female / adult / [england]` default; for a `--narrator-clip` override it is the clip's own gender, age band and accent. For skipped characters it is a placeholder `male / adult` that nothing reads. |
| `candidates_considered` | `int` | `0` | How many clips survived the filters and were shown to the ranking call (1 for an override). |
| `ranked` | `list[_RankedChoice]` | `[]` | The LLM's top-N (default 3) in order, minus any id that was not offered; a single fallback entry when none was. Kept so a reviewer can swap to the runner-up without re-running the LLM. Not updated by the audit. |
| `assigned_clip_id` | `str` | `""` | The clip actually used. Empty means "no reference clip": s4 renders that character descriptor-only. |
| `voice_descriptor` | `str` | `""` | Optional pre-computed Dramabox descriptor. When non-empty (normally only the Narrator: override, auto-cast, or inherited), Stage 3 uses it verbatim instead of asking the LLM to write one, so the descriptor cannot contradict the clip. Kept by a re-cast only while the clip is unchanged; emptied by the audit when it moves a character. |
| `locked` | `bool` | `False` | Hand-set flag for a deliberate assignment. `lnvox voice audit --fix` still reports this casting's collisions but never reassigns it (DESIGN.md §20.4), and `lnvox voice cast` keeps the entry exactly as it is, over prior volumes, `--narrator-clip` and `--fresh`. |
| `audit_note` | `str` | `""` | Free-text provenance. The audit writes `voice audit: was 0.7 semitones from <other>, who shares its scenes[ (adjacent age band)…]` when it moves a character. `voice cast` writes `voice cast: …` when it had to fall back (no offered id in the ranking, or a locked clip missing from the bank). You can also use it to note why something is locked. |

:::note[What survives a re-cast]
`lnvox voice cast` rewrites the whole file, but reads it first. A locked entry is copied as it is, whatever prior volumes or `--narrator-clip` say. An unlocked entry is copied whole when no prior volume knows the character. When a prior volume or `--narrator-clip` assigns a clip, the entry's `audit_note` and `voice_descriptor` survive only if that clip is the one it already had. So a `"locked": true` added at the `--stop-after-audit` checkpoint is still there when the resumed run reaches Stage A. See [Stage V](/stages/voice-cast/#cross-volume-reuse).
:::

### `BookCasting`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `book_id` | `str` | required | The book id the casting was made for, e.g. `myseries/volume-02`. |
| `castings` | `list[CharacterCasting]` | `[]` | One entry per `01_characters.json` character, in cast order, plus a synthesised `Narrator` appended last when the cast has none. |

## File formats

### `<voicebank>/manifest.json`

`Voicebank.model_dump_json(indent=2)`. The voicebank directory is `$LNVOX_VOICEBANK` (default `voicebank`); clip paths are relative to it.

```json
{
  "version": "1",
  "clips": [
    {
      "id": "cv_051e865815e5",
      "source": "common_voice",
      "clip_path": "clips/cv_051e865815e5.wav",
      "duration_seconds": 13.39,
      "gender": "female",
      "age_band": "adult",
      "accent": "england",
      "sample_sentences": [
        "It has been called the center of Cherokee culture."
      ],
      "license": "CC0",
      "notes": "Common Voice (validated.tsv); speaker 051e8658…"
    }
  ]
}
```

The same directory holds `clips/<id>.wav` (mono 24 kHz PCM-16 for seeded clips) and, once the audit has run, the derived cache `acoustics.json` ([lnvox.voices.acoustics](/library/voices/acoustics/)).

### `artifacts/<book_id>/04_voice_assignments.json`

`BookCasting.model_dump_json(indent=2)`, written by `lnvox voice cast` and rewritten in place by `lnvox voice audit --fix` and the Studio's Casting tab. Names below are invented.

```json
{
  "book_id": "myseries/volume-02",
  "castings": [
    {
      "character_name": "Aldric Vane",
      "target": {
        "gender": "male",
        "age_band": "young_adult",
        "accent_keywords": ["any"],
        "timbre_keywords": ["warm", "baritone"],
        "manner_keywords": ["measured"]
      },
      "candidates_considered": 60,
      "ranked": [
        {"clip_id": "cv_3f1a0c9e22b1", "score": 0.92, "reason": "Calm, measured delivery fits a veteran captain."},
        {"clip_id": "cv_a07d51e4c3f0", "score": 0.85, "reason": "Warm low register."},
        {"clip_id": "cv_9b2e7d11aa04", "score": 0.78, "reason": "Clear, formal phrasing."}
      ],
      "assigned_clip_id": "cv_3f1a0c9e22b1",
      "voice_descriptor": "",
      "locked": false,
      "audit_note": ""
    },
    {
      "character_name": "Mira",
      "target": {"gender": "female", "age_band": "teen", "accent_keywords": ["any"],
                 "timbre_keywords": ["bright"], "manner_keywords": ["energetic"]},
      "candidates_considered": 22,
      "ranked": [
        {"clip_id": "cv_77c0e1b9d3a2", "score": 0.9, "reason": "Bright, quick delivery."}
      ],
      "assigned_clip_id": "cv_c41f08a2b6e9",
      "voice_descriptor": "",
      "locked": false,
      "audit_note": "voice audit: was 0.6 semitones from Sera Kalt, who shares its scenes"
    },
    {
      "character_name": "The Warden",
      "target": {"gender": "male", "age_band": "adult", "accent_keywords": [],
                 "timbre_keywords": [], "manner_keywords": []},
      "candidates_considered": 0,
      "ranked": [],
      "assigned_clip_id": "",
      "voice_descriptor": "",
      "locked": false,
      "audit_note": ""
    },
    {
      "character_name": "Narrator",
      "target": {"gender": "female", "age_band": "adult", "accent_keywords": ["england"],
                 "timbre_keywords": [], "manner_keywords": []},
      "candidates_considered": 1,
      "ranked": [
        {"clip_id": "cv_051e865815e5", "score": 1.0, "reason": "Manual --narrator-clip override."}
      ],
      "assigned_clip_id": "cv_051e865815e5",
      "voice_descriptor": "adult, British, female, warm clear voice, measured and engaging storyteller",
      "locked": false,
      "audit_note": ""
    }
  ]
}
```

Reading the example: `Mira` was moved by the audit (her `assigned_clip_id` no longer matches `ranked[0]`, and `audit_note` says why); `The Warden` has a `nonbinary`/`unknown` gender in `01_characters.json`, so he was skipped with a placeholder target and no clip; the Narrator came from `--narrator-clip`.

### Who writes which field

| Writer | Fields it sets |
|---|---|
| `lnvox voice cast` ([cast_book](/library/voices/matcher/#cast_book)) | everything. An entry kept from this volume's own file keeps its `locked`, `audit_note` and `voice_descriptor`; fresh castings get `locked: false` and an `audit_note` only for a fallback |
| `lnvox voice audit --fix` ([repair_collisions](/library/voices/audit/#repair_collisions)) | `assigned_clip_id`, `audit_note`, and an emptied `voice_descriptor`, on moved characters only |
| ComfyUI Set Voice node | `assigned_clip_id`, `locked` (on by default), `audit_note`; empties `voice_descriptor` |
| Voicebank Studio, Casting tab | `assigned_clip_id` only; everything else round-trips |
| You, by hand | typically `assigned_clip_id` and `locked` |

## Notes

- Files written before `locked` / `audit_note` existed still validate; the fields default to `False` / `""`.
- `character_name` must match the `speaker` strings in `02_scenes/` / `03_directed/` exactly for the audit and s4 to connect a beat to its voice. The Narrator is always the literal `Narrator`.
- Changing `assigned_clip_id` after s3 leaves the character's Stage 3 descriptor describing the old voice; after s4 it also needs a re-render (`lnvox audio purge`, see [utilities](/stages/utilities/)). See DESIGN.md §6 and §20.
