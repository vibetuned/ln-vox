---
title: lnvox.llm.prompts
description: The 14 Jinja prompt templates — how they are loaded and rendered, which module renders each, its variables, its output schema and what it instructs.
sidebar:
  order: 4
  label: prompts
---

`src/lnvox/llm/prompts/` holds the user-message templates for every LLM call except the chunker's break-point prompt, which is built inline in [lnvox.llm.chunker](/library/llm/chunker/#_ask_llm_for_break). The **system** messages are Python constants in the calling module (`s1_characters.SYSTEM`, `s2_scenes.BOUNDARY_SYSTEM`, …). Templates carry the task instructions, an explicit JSON shape, and the data. Edit these when you tune model behaviour, and read [Notes](#notes) first: most stage caches don't notice template changes.

## At a glance

| Template | Rendered by | Output schema | Stage |
| --- | --- | --- | --- |
| [`block_classify.jinja`](#block_classifyjinja) | `ingest.blocks._resolve_ambiguous` | `BlockClass` | [ingest-epub](/stages/ingest-epub/) (lecture) |
| [`characters_per_chapter.jinja`](#characters_per_chapterjinja) | `s1_characters.extract_per_chapter` | `CharacterList` | [s1](/stages/s1-characters/) |
| [`characters_merge.jinja`](#characters_mergejinja) | `s1_characters.merge_clusters` | `CharacterMergeProposal` | s1 |
| [`scene_boundaries.jinja`](#scene_boundariesjinja) | `s2_scenes._detect_boundaries` | `ChapterBoundaries` | [s2](/stages/s2-scenes/) |
| [`scene_beats.jinja`](#scene_beatsjinja) | `s2_scenes._tag_scene_beats` | `SceneBeats` | s2 |
| [`voice_target.jinja`](#voice_targetjinja) | `voices.matcher.infer_target` | `VoiceTarget` | [voice cast](/stages/voice-cast/) |
| [`voice_match.jinja`](#voice_matchjinja) | `voices.matcher.rank_candidates` | `MatchResult` | voice cast |
| [`voice_profiles.jinja`](#voice_profilesjinja) | `s3_director.generate_voice_profiles` | `VoiceProfileList` | [s3](/stages/s3-director/), [scenario](/stages/scenario/) |
| [`scene_directions.jinja`](#scene_directionsjinja) | `s3_director.direct_scene` | `SceneDirections` | s3 |
| [`lecture_normalize.jinja`](#lecture_normalizejinja) | `lecture._normalize_beats` | `NormalizedBeats` | [lecture](/stages/lecture/) |
| [`scenario_structure.jinja`](#scenario_structurejinja) | `ingest.scenario.structure_chunk` | `SceneStructure` | [ingest-scenario](/stages/ingest-scenario/) |
| [`scenario_roster.jinja`](#scenario_rosterjinja) | `ingest.scenario.extract_roster` | `RosterList` | ingest-scenario |
| [`scenario_characters.jinja`](#scenario_charactersjinja) | `ingest.scenario.extract_characters` | `CharacterList` | ingest-scenario |
| [`scenario_directions.jinja`](#scenario_directionsjinja) | `stages.scenario.direct_scene` | `ScenarioDirections` | scenario |

Schemas are in [lnvox.llm.schemas](/library/llm/schemas/), except `VoiceTarget` and `MatchResult`, which are in [lnvox.voices.schema](/library/voices/schema/).

## Loading and rendering

The loader is a module-level Jinja environment in `src/lnvox/llm/client.py`:

```python title="src/lnvox/llm/client.py"
_PROMPTS_DIR = Path(__file__).parent / "prompts"
_jinja = Environment(
    loader=FileSystemLoader(_PROMPTS_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
    autoescape=False,
)
```

Templates are rendered with [`LLMClient.render(template, **kwargs)`](/library/llm/client/#render), which is `_jinja.get_template(template).render(**kwargs)`. `ingest/blocks.py` receives that bound method as its `render_template` callback, so it never imports the client.

- `StrictUndefined`: a variable the caller forgot raises `jinja2.UndefinedError` at render time. A typo can't produce a silently empty prompt.
- `autoescape=False`: JSON payloads and quotes are inserted verbatim.
- `trim_blocks` / `lstrip_blocks`: `{% … %}` tags leave no stray newlines or indentation. Only `scenario_directions.jinja` and `voice_profiles.jinja` use block tags.
- Payloads are pre-serialised by callers with `json.dumps(..., ensure_ascii=False, indent=2)`, so non-ASCII names reach the model unescaped.

The rendered string becomes the `user` message. Callers then usually size the budget with [`budget_for`](/library/llm/client/#budget_for) and call [`structured()`](/library/llm/client/#structured) with the schema below.

## Templates

### `block_classify.jinja`

- **Rendered by:** `lnvox.ingest.blocks._resolve_ambiguous`, only for blocks the deterministic rules can't classify, and only in lecture mode with `--ingest-classifier fallback`.
- **Variables:** `block_html` (the element's HTML, truncated to 2000 chars).
- **Schema:** `BlockClass`, with `max_tokens=256`. On any failure the block is treated as prose.
- **Instructions:** classify one EPUB block as `prose` (the default), `code`, `table`, `figure`, `equation`, `footnote`, or `drop` (page numbers, running headers, stray TOC). Return `{"kind", "reason"}`.

### `characters_per_chapter.jinja`

- **Rendered by:** `lnvox.stages.s1_characters.extract_per_chapter`.
- **Variables:** `title`, `text` (the chapter title and full chapter text).
- **Schema:** `CharacterList`, with `budget_for(desired=24576, floor=8192)` and a salvage parser for truncated arrays.
- **Instructions:** list every character who appears, speaks or acts, skipping groups, pure mentions and names from the examples. Give each a most-used proper name, distinctive aliases only (no pronouns or generic roles), gender, age band, a 2–4 sentence description (physical, voice, manner, **affiliation**, origin), and 1–3 verbatim evidence quotes. The top-level key must be `characters`.

### `characters_merge.jinja`

- **Rendered by:** `lnvox.stages.s1_characters.merge_clusters`. It's skipped for fewer than two clusters or when the payload exceeds s1's input budget, in which case the merge is deterministic only.
- **Variables:** `clustered_cast_json` (compact JSON of the deterministically clustered draft cast: name, aliases, gender, approx_age, description).
- **Schema:** `CharacterMergeProposal`, with `budget_for(desired=4096, floor=1024)`. On failure s1 falls back to the deterministic merge.
- **Instructions:** return only groups of differently named entries that are the same person. Be conservative: a shared role or title isn't evidence, never group across gender or incompatible ages, and when in doubt don't merge. Use `{"merges": []}` when there's nothing to merge.

### `scene_boundaries.jinja`

- **Rendered by:** `lnvox.stages.s2_scenes._detect_boundaries`, once per [chunk](/library/llm/chunker/#chunk_text).
- **Variables:** `cast_json` (name + aliases per character), `chapter_id`, `title` (`"<title> (part i/n)"` for multi-chunk chapters), `paragraph_count`, `numbered_paragraphs` (`[1]\n<para>` blocks, numbered locally within the chunk).
- **Schema:** `ChapterBoundaries`, with `budget_for(desired=max(1024, min(8192, 80 × paragraphs + 512)), floor=1024)` tokens.
- **Instructions:** split the chapter into scenes on shifts of location, time, section break or POV. Scenes must tile paragraphs 1..N with no gaps or overlaps (the code repairs any that don't, see [`_tile_boundaries`](/library/stages/s2-scenes/#_tile_boundaries)), and fewer, longer scenes are preferred. Each scene carries `scene_id` `<chapter_id>_s<N>`, `location_hint` and the canonical `cast` present.

### `scene_beats.jinja`

- **Rendered by:** `lnvox.stages.s2_scenes._tag_scene_beats`, once per scene.
- **Variables:** `cast_json`, `scene_id`, `location_hint`, `text` (the scene's paragraphs joined by blank lines).
- **Schema:** `SceneBeats`, with `budget_for(desired=max(4096, min(32000, int(len(text) × 2.0) + 1024)), floor=4096)` tokens.
- **Instructions:** break the scene into ordered `narration` / `dialogue` beats. Dialogue `text` is spoken words only (quotes and "said X" removed), and `speaker` is the exact cast `name` or `"Unknown"`. Every beat also carries `source_span`, an exact contiguous substring of the scene including quotes and tags. Read top to bottom, the spans must reconstruct the scene.

### `voice_target.jinja`

- **Rendered by:** `lnvox.voices.matcher.infer_target`.
- **Variables:** `name`, `gender`, `approx_age`, `description` (from the `Character`), `voice_descriptor` (from an existing profile, else `""`), `profile_accent` (else `"any"`).
- **Schema:** `VoiceTarget`, with `max_tokens=1024`.
- **Instructions:** produce voicebank search metadata: `gender` (male/female), `age_band` (teen … elder), `accent_keywords` (Common Voice codes; use `profile_accent` directly, `["any"]` when it's `any`), and short `timbre_keywords` / `manner_keywords`. Everything must be grounded in the description.

### `voice_match.jinja`

- **Rendered by:** `lnvox.voices.matcher.rank_candidates`, after the hard gender + age-band filter and the soft accent sort.
- **Variables:** `character_json`, `target_json` (the `VoiceTarget`), `candidates_json` (id, accent, first sample sentence truncated to 160 chars, duration), `candidate_count`, `top_n`.
- **Schema:** `MatchResult`, with `max(1024, 200 × top_n + 512)` tokens.
- **Instructions:** pick the `top_n` clips whose accent, timbre and delivery best fit the character, using exact candidate ids. Prefer accent match and a register-appropriate sample sentence, and keep the ranking diverse. Return `ranked_choices` with `clip_id`, `score` and `reason`.

### `voice_profiles.jinja`

- **Rendered by:** `lnvox.stages.s3_director.generate_voice_profiles`, through `refresh_voice_profiles` from both `s3_director.run` and `stages.scenario.run`. Only characters without a current cached profile or a locked casting descriptor are sent; it's skipped entirely when there are none.
- **Variables:** `cast_with_clips_json` (per character: `name`, `description`, `assigned_clip` with `id` / `gender` / `age_band` / `accent`, or `null`); `includes_narrator` (whether the Narrator is among them, default true: the "one entry named Narrator" paragraph is shown only then).
- **Schema:** `VoiceProfileList`, with `budget_for(desired=max(4096, 400 × n + 1024), floor=4096)` and a salvage parser. Gender, age band and accent are snapped back to the assigned clip afterwards.
- **Instructions:** write one Dramabox descriptor per character, `"<age band> <accent adjective> <gender>, <pitch/timbre>, <habitual manner>"`. It must be consistent with the assigned clip's gender, age and accent (e.g. `england` → "British"), with timbre and manner taken from the personality. No name, no period, no quotes. The `Narrator` entry follows the same rules. Exactly one profile per listed entry, and no others.

### `scene_directions.jinja`

- **Rendered by:** `lnvox.stages.s3_director.direct_scene`, per scene with at least one dialogue line.
- **Variables:** `scene_id`, `location_hint`, `scene_context` (narration as `(narration) …`, dialogue as `[L<n>] [<speaker>] "…"`), `numbered_dialogue` (`<n>. [<speaker>] "…"`).
- **Schema:** `SceneDirections`, with `budget_for(desired=max(2048, 40 × lines + 512), floor=2048)`. On failure the lines get no cue.
- **Instructions:** write one 2–8 word cue per numbered line, covering the speaker's vibe plus any vocal action ("panicked, voice cracking"). It must be grounded in the narration, never repeat the line's words, and skip no line.

### `lecture_normalize.jinja`

- **Rendered by:** `lnvox.stages.lecture._normalize_beats`, in batches of 25 beats. It's skipped with `lnvox lecture --no-normalize`.
- **Variables:** `passages` (`[<i>] <source_span>` lines, 0-indexed within the batch).
- **Schema:** `NormalizedBeats`, with `budget_for(desired=max(2048, chars / 2 + 1024), floor=2048)`. A failed batch stays verbatim.
- **Instructions:** respell each passage for the ear only: numbers, dates, units, currency, symbols and abbreviations, plus speakable URLs. Never paraphrase, reorder, add or drop words, and return an entry for every index.

### `scenario_structure.jinja`

- **Rendered by:** `lnvox.ingest.scenario.structure_chunk`, per scene chunk part, through the `cache/scenario/` content cache.
- **Variables:** `chunk` (the cleaned markdown of one scene part).
- **Schema:** `SceneStructure`, with `budget_for(desired=max(4096, int(len(chunk) × 1.2)), floor=4096)`. Dialogue that isn't verbatim is demoted to staging afterwards.
- **Instructions:** classify every line as `dialogue` (speaker label as printed), `staging` or `cue` (sound, light, technical), with text verbatim minus markdown. Keep the order, split a line around an embedded italic stage direction, omit headers, and prefer staging when unsure. It includes an invented French example.

### `scenario_roster.jinja`

- **Rendered by:** `lnvox.ingest.scenario.extract_roster`, once per detected characters section, through the content cache.
- **Variables:** `chunk` (the characters-section markdown).
- **Schema:** `RosterList`, with `budget_for(desired=4096, floor=2048)`.
- **Instructions:** one entry per role (several roles may share a performer), with `name`, verbatim `description` and `actor` from arrow or slash notation. Copy text exactly and never invent.

### `scenario_characters.jinja`

- **Rendered by:** `lnvox.ingest.scenario.extract_characters`, through the content cache. It's skipped when there are no non-group speakers.
- **Variables:** `speakers_json` (per role: `name`, `bio` from the roster, up to two `sample_lines` of 160 chars each).
- **Schema:** `CharacterList`, with `budget_for(desired=max(4096, 200 × n + 1024), floor=4096)`. Names come from the script and the LLM only fills attributes. Roles it omits get a stub.
- **Instructions:** for each role, copy `name` exactly and infer `gender` / `approx_age` from bio, name and grammatical gender. Write a 1–2 sentence voice-casting `description` in the script's language, and leave `aliases` / `evidence` empty.

### `scenario_directions.jinja`

- **Rendered by:** `lnvox.stages.scenario.direct_scene`, per script scene with dialogue.
- **Variables:** `scene_id`, `scene_title` (used in an `{% if %}`), `scene_context` (dialogue as `[L<n>] [<speaker>] "…"`, staging as `(didascalie) …`, cues omitted), `numbered_dialogue`.
- **Schema:** `ScenarioDirections`, with `budget_for(desired=max(2048, 50 × lines + 512), floor=2048)`. On failure every line is `calm` with no cue.
- **Instructions:** for each numbered line, give one of the 7 emotions plus a 2–6 word cue **in the script's language**. Ground it in the staging directions, prefer playable cues over "calme", and output exactly one entry per line.

## Notes

:::caution[Editing a template does not invalidate most caches]
s1 (`01_characters_per_chapter/`), s2 (`02_scenes/`), s3 (`03_directed/`, `03_voice_profiles.json`), lecture and scenario (`03_directed/`) reuse any valid artifact already on disk. None of them key on the prompt text (the s3/scenario/lecture caches key on the casting and settings, not the templates). After changing one of their templates, delete the affected artifacts (or pass `--regen-profiles` to `lnvox s3` / `lnvox scenario` for profiles) to see the effect. Only `ingest-scenario`'s `cache/scenario/` keys on the **rendered** prompt (plus schema name and model), so its three templates re-key automatically.
:::

- **Keep the JSON example and the schema in sync.** Each template spells out the exact output shape, which matters on llama-server and mlx-lm where `guided_json` isn't enforced. If you rename a schema field, update the example, or the model will keep emitting the old key. Some old spellings are accepted on purpose through `AliasChoices` (see [CharacterList](/library/llm/schemas/#characterlist)).
- **Examples leak.** `characters_per_chapter.jinja` explicitly forbids names taken from its own example (`Sir Patrick`), and s1 additionally drops characters not attested in the chapter text. Keep example names out of real books, and prefer obviously invented examples, as the scenario templates do.
- **IP rule (scenario mode).** DESIGN.md §17 forbids embedding any real script text in prompts, code or tests. The scenario templates' examples are invented for that reason.
- Templates are English even for French scripts. The scenario templates ask for descriptions and cues *in the script's language*, while the TTS prompt itself stays English (`"<descriptor>, <emotion>"`).
