---
title: lnvox.stages.s2_scenes
description: Stage 2 scene and speaker segmentation. Pass 2a finds paragraph-range scene boundaries, pass 2b tags beats with verbatim source_span, cached per chapter.
sidebar:
  order: 2
  label: s2_scenes
---

Stage 2 turns each chapter into scenes, and each scene into an ordered list of narration and dialogue **beats**. Every beat carries a speaker and a verbatim `source_span`. It makes two LLM passes per chapter: 2a picks scene boundaries over numbered paragraphs, and 2b tags beats one scene at a time. `lnvox s2` ([stage page](/stages/s2-scenes/)) calls `run()`. The output feeds [s3](/library/stages/s3-director/), the [casting audit](/stages/voice-audit/) (scene co-occurrence) and, through `source_span`, the [sync stage](/library/stages/s6-sync/).

## At a glance

| Symbol | Kind | Purpose |
| --- | --- | --- |
| `run` | function | Stage entry point with a per-chapter cache in `02_scenes/` |
| `segment_chapter` | function | Chunk, then pass 2a, then pass 2b, then speaker normalisation, for one chapter |
| `_detect_boundaries` | function | Pass 2a for one chunk; returns chapter-global `SceneBoundary` ranges |
| `_tile_boundaries` | function | Repairs 2a's ranges so the chapter's scenes cover every paragraph exactly once |
| `_tag_scene_beats` | function | Pass 2b for one scene; returns `SceneBeats` |
| `_tag_in_halves` | function | Pass 2b with a fallback: a scene whose reply never validates is tagged in halves |
| `_build_alias_map` / `_normalize_speakers` | functions | Rewrite dialogue speakers that exactly match a cast alias |
| `_looks_broken` | function | Detects degenerate cached results (a scene with zero beats) |
| `BOUNDARY_SYSTEM`, `BEATS_SYSTEM` | constants | System prompts for 2a and 2b |

Schemas (`SceneBoundary`, `ChapterBoundaries`, `Beat`, `SceneBeats`, `Scene`, `ChapterScenes`) are documented in [lnvox.llm.schemas](/library/llm/schemas/). Paragraph splitting and chunking come from [lnvox.llm.chunker](/library/llm/chunker/).

## API

### `run`

```python
def run(
    chapters: list[Chapter],
    cast: CharacterList,
    client: LLMClient,
    output_dir: Path,
    *,
    on_chapter_done=None,
) -> list[ChapterScenes]
```

Segments every chapter and returns the results in chapter order.

For each chapter it looks for `02_scenes/<chapter_id>.json`. A cached file is used as-is unless it fails to parse, or unless `_looks_broken` flags it. In that case it prints `[s2] cached <id> has empty beats (silent-failure carryover) — re-segmenting` and segments again. Otherwise it calls `segment_chapter` and writes the file. `on_chapter_done(chapter, ChapterScenes)` is called for cached and fresh chapters alike.

**Parameters**
- `chapters`: chapters from `00_text.jsonl`.
- `cast`: the Stage 1 cast. Only `name` and `aliases` are sent to the model.
- `client`: the `LLMClient`.
- `output_dir`: `artifacts/<book_id>/`. `02_scenes/` is created under it.
- `on_chapter_done`: optional `callable(chapter, ChapterScenes)`.

**Writes**: `02_scenes/<chapter_id>.json`.

### `segment_chapter`

```python
def segment_chapter(
    client: LLMClient, chapter: Chapter, cast: CharacterList
) -> ChapterScenes
```

Segments one chapter. The steps are:

1. **Cast summary.** `[{"name", "aliases"}]` for every cast member, rendered as JSON into both prompts.
2. **Paragraphs.** `split_paragraphs(chapter.text)` splits on blank lines (`\n\n`) and drops whitespace-only paragraphs. This list is the single source of truth for paragraph numbering.
3. **Chunks.** `chunk_text(client, chapter.text)` returns paragraph-aligned chunks, targeting 30 000 characters, with LLM-chosen break points (see [chunker](/library/llm/chunker/)). A chapter of 30 000 characters or less is one chunk.
4. **Pass 2a.** For each chunk, `_detect_boundaries`. When there are several chunks, the title sent to the model gets a ` (part i/n)` suffix. Boundaries from all chunks are concatenated, then `_tile_boundaries` repairs them to tile the chapter's paragraphs exactly once. Each repair is printed as `[s2] <chapter>: scene ranges — <note>`.
5. **Pass 2b.** For each boundary, in order:
   - it assigns `scene_id = "<chapter_id>_s<N>"` sequentially, overwriting whatever the model returned;
   - it slices `paragraphs[start_paragraph - 1 : end_paragraph]` and joins them with `\n\n`;
   - it skips the scene if that text is empty (the number is not consumed);
   - it calls `_tag_in_halves`, which calls `_tag_scene_beats`. If the model returns **zero beats** for non-empty text, it raises `RuntimeError("s2: scene … returned 0 beats … likely a stalled LLM …")`.
6. **Speaker normalisation.** `_normalize_speakers` rewrites dialogue speakers that exactly match a cast alias.

The persisted `Scene` keeps `scene_id`, `location_hint`, `start_paragraph`, `end_paragraph` and `beats`. The 2a `cast` list is used only in the prompt and is not stored.

### `_detect_boundaries`

```python
def _detect_boundaries(
    client: LLMClient,
    chapter: Chapter,
    chunk: Chunk,
    *,
    cast_json: str,
    title: str,
) -> list[SceneBoundary]
```

Pass 2a over one chunk. It renders `scene_boundaries.jinja` with the chunk's paragraphs numbered **locally** as `[1]`, `[2]`, … `[N]`, and asks for `ChapterBoundaries`. The budget is `budget_for(desired=max(1024, min(8192, 80 * N + 512)), floor=1024)`: clamped to what the served context leaves after the prompt, which can be about 40 000 characters for a full chunk.

Post-processing for each returned boundary:

- `start = clamp(start_paragraph, 1, N)` and `end = clamp(end_paragraph, start, N)`;
- both are then offset by `chunk.base_paragraph`, which makes them **chapter-global** and 1-indexed.

So in a chapter whose second chunk starts at paragraph 96 (`base_paragraph = 95`), a model answer of `start_paragraph: 1, end_paragraph: 40` becomes `96–135`.

If the model returns an empty `scenes` list, the whole chunk becomes one scene (`<chapter_id>_s1`, empty `location_hint`). Exceptions from `structured` (a dead server, repeated truncation) **propagate**, so the pipeline retry can restart the server.

### `_tile_boundaries`

```python
def _tile_boundaries(
    boundaries: list[SceneBoundary], n_paragraphs: int
) -> tuple[list[SceneBoundary], list[str]]
```

The prompt demands that scenes tile the chapter with no gaps and no overlaps, and this enforces it. Pass 2b only tags the paragraphs inside a scene's range, so a gap would leave paragraphs out of the audio and an overlap would voice them twice. On copies of the boundaries it:

1. clamps every range into `1..n_paragraphs`;
2. sorts by `(start_paragraph, end_paragraph)`;
3. walks them in order. A gap before a scene is folded into the **previous** scene (a gap at the head of the chapter into the first scene). An overlap is trimmed off the start of the later scene, and a scene the previous one already covers is dropped;
4. stretches the last scene to the final paragraph.

It returns the tiled list and one human-readable note per repair (`gap 9–11 folded into the previous scene`, `overlap 3–4 trimmed off 3–7`, `tail 20–22 folded into the last scene`, …). With `n_paragraphs == 0` or no boundaries, it changes nothing.

```python
_tile_boundaries([b(1, 8), b(12, 20)], 22)
# ([1–11, 12–22], ["gap 9–11 folded into the previous scene", "tail 21–22 folded into the last scene"])
```

### `_tag_scene_beats`

```python
def _tag_scene_beats(
    client: LLMClient,
    boundary: SceneBoundary,
    scene_text: str,
    *,
    cast_json: str,
) -> SceneBeats
```

Pass 2b for one scene. It renders `scene_beats.jinja` (cast, `scene_id`, `location_hint`, scene text) and asks for `SceneBeats`. The prompt contract is:

- `narration` beats hold prose, including internal thoughts;
- `dialogue` beats have `text` = the spoken words only, with quote marks and attribution tags removed, and `speaker` = the cast `name`, or `"Unknown"` when the line can't be attributed;
- every beat has `source_span`: the verbatim, contiguous slice of the scene text, including the quote marks and the dropped "she said" tag. Read top to bottom, the spans should rebuild the scene.

The budget is `budget_for(desired=max(4096, min(32000, int(len(scene_text) * 2.0) + 1024)), floor=4096)`: the scene text is in the prompt, so on a smaller context the 32 000 cap alone would ask for more than fits and return a 400. Beats JSON carries the source twice, measured at about 5.2 times the scene length in characters. If a completion is still cut off (`finish_reason=length`), `LLMClient.structured` grows the budget by 1.5 times on the next attempt. Exceptions propagate.

### `_tag_in_halves`

```python
def _tag_in_halves(
    client: LLMClient,
    boundary: SceneBoundary,
    paragraphs: list[str],
    *,
    cast_json: str,
    depth: int = 0,
) -> list[Beat]
```

Calls `_tag_scene_beats` on the scene's paragraphs, joined by blank lines. If that raises `ValueError` (the client's "failed schema validation" after all its attempts), it prints `[s2] <scene_id>: no valid beats for N paragraph(s) (…) — tagging the two halves apart`, splits the paragraphs at the middle, and tags each half the same way. It recurses to depth 3, so the smallest piece is about 1/8 of the scene. The halves' beats are concatenated in order; this works because a scene is contiguous paragraphs and each beat carries its own `source_span`. A single paragraph, or depth 3, re-raises.

This exists because the quantized model sometimes loops on one scene, emitting `{}` pairs until `finish_reason=length`, and loops again on every retry of the same prompt. The launcher's whole-stage retries then failed on the same scene, which stopped the volume. Shorter prompts get through. Other exceptions (transport errors, timeouts) are not caught.

### `_build_alias_map` / `_normalize_speakers`

```python
def _build_alias_map(cast: CharacterList) -> dict[str, str]
def _normalize_speakers(scenes: ChapterScenes, alias_map: dict[str, str]) -> int
```

`_build_alias_map` maps each lowercased, stripped name or alias to the canonical `name`. If two characters share an alias, the last one wins. `_normalize_speakers` rewrites, in place, every dialogue beat whose lowercased speaker is in the map, and returns the number of beats it rewrote. Unmatched names, including `"Unknown"`, are left alone. This is an exact match only. Article-insensitive matching ("The Square" → "A SQUARE") and the `narrator` fold happen later, in [s3's `canonicalize_scene_speakers`](/library/stages/s3-director/#canonicalize_scene_speakers).

### `_looks_broken`

```python
def _looks_broken(result: ChapterScenes) -> bool
```

True when the result has no scenes, or any scene has zero beats. Empty scenes are filtered out before beat tagging and a zero-beat answer now raises, so this can only match a cache written by an older, silently-failing run. Those runs cached `[scene(beats=[])]` when the server died, which corrupted every later chapter.

## Output artifact

```json
{
  "chapter_id": "03",
  "scenes": [
    {
      "scene_id": "03_s1",
      "location_hint": "Vex's study at dusk",
      "start_paragraph": 1,
      "end_paragraph": 19,
      "beats": [
        {"type": "narration", "text": "The duke turned from the window.",
         "speaker": null, "source_span": "The duke turned from the window."},
        {"type": "dialogue", "text": "You're late.", "speaker": "Lord Vex",
         "source_span": "“You're late,” Vex said without turning."},
        {"type": "dialogue", "text": "I came as soon as I could.", "speaker": "Lady Rosalind",
         "source_span": "“I came as soon as I could.”"}
      ]
    },
    {
      "scene_id": "03_s2",
      "location_hint": "the courtyard, next morning",
      "start_paragraph": 20,
      "end_paragraph": 41,
      "beats": [
        {"type": "narration", "text": "Frost had crept across the stones overnight.",
         "speaker": null, "source_span": "Frost had crept across the stones overnight."}
      ]
    }
  ]
}
```

`text` is lossy: quotes are stripped, attribution is dropped, whitespace is collapsed. `source_span` is lossless. The two differ exactly where the cleanup happened, which is why the sync stage anchors on `source_span` (DESIGN.md §2.3, §2.8).

## Notes

- **Paragraph numbers are 1-indexed and chapter-global.** Scene N covers `split_paragraphs(chapter.text)[start-1:end]`. (Lecture mode's `DirectedBeat.source_paragraph` is a separate, 0-indexed field.)
- **Caching.** The cache is per chapter and keyed only on the file's presence. Re-running s1 does not invalidate `02_scenes/`. Speaker tags written against an old cast stay until you delete the chapter files. s3's speaker canonicalisation repairs article and alias mismatches, but not a renamed character.
- **Chunk breaks.** `_ask_llm_for_break` in the chunker swallows its own errors and falls back to the middle candidate paragraph, so chunking never fails a chapter. Boundary and beat calls do.
- Design rationale for the two passes and `source_span`: DESIGN.md §2.3.
