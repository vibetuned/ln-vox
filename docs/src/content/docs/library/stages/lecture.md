---
title: lnvox.stages.lecture
description: Lecture mode Stage L. Deterministic narration beat split plus an optional LLM speech-normalize pass, writing 03_directed/ for s4 unchanged.
sidebar:
  order: 4
  label: lecture
---

Stage L replaces s1, s2 and s3 for non-fiction and technical books. There are no characters, no scene detection and no Director. Each chapter's prose is split deterministically into single-voice narration beats. An optional LLM pass then respells each beat's `text` for the ear, while `source_span` stays verbatim. The output is ordinary `03_directed/<chapter_id>.json`, so [s4](/library/stages/s4-tts/), [s5](/library/stages/s5-mix/) and [s6](/library/stages/s6-sync/) run unchanged. `lnvox lecture` ([stage page](/stages/lecture/)) calls `run()`.

## At a glance

| Symbol | Kind | Purpose |
| --- | --- | --- |
| `run` | function | Stage entry point, cached per chapter (redone when the normalize setting or narrator changes) |
| `direct_chapter` | function | Split, then normalize, then prompts, for one chapter |
| `split_chapter` | function | Deterministic paragraph and sentence split into narration beats |
| `narrator_descriptor` | function | The Narrator's descriptor from the casting, or the default |
| `_normalize_beats` | function | Batched, best-effort speech-normalize of `text` |
| `_NORMALIZE_BATCH` | constant | `25` beats per normalize call |
| `NORMALIZE_SYSTEM` | constant | System prompt for the normalize call |

This module imports `MAX_MERGED_BEAT_CHARS` (375), `_split_long_text`, `format_prompt`, `NARRATOR_NAME` and `DEFAULT_NARRATOR_DESCRIPTOR` from [s3_director](/library/stages/s3-director/), so lecture beats follow exactly the same length policy and prompt format as narration mode. Paragraphs come from `split_paragraphs` in [lnvox.llm.chunker](/library/llm/chunker/).

## API

### `run`

```python
def run(
    chapters: list[Chapter],
    output_dir: Path,
    *,
    casting: Optional[BookCasting] = None,
    client=None,
    normalize: bool = True,
    on_chapter_done: Callable[[ChapterDirected], None] | None = None,
) -> list[ChapterDirected]
```

Resolves the narrator descriptor once, with `narrator_descriptor(casting)`, and the effective normalize setting, `normalize and client is not None`. Then, for each chapter, it reloads `03_directed/<chapter_id>.json` if present and parseable and checks it against this run:

- **Normalize setting differs** from the file's `normalized` record (`--no-normalize` toggled): the chapter is rebuilt with `direct_chapter`, printing `[lecture] <chapter>: cached with/without speech-normalize — rebuilding …`.
- **Narrator descriptor differs** from the file's `voice_descriptors` record: every beat's `direction` and `prompt` are re-composed with the new descriptor. The (normalized) `text` doesn't depend on the voice, so no LLM call is made.
- **No record** (a file from before the records existed): treated as current and stamped with this run's values.

A missing or unparseable file is built with `direct_chapter(chapter, descriptor, client=client, normalize=normalize)`. A file is written only when it changed.

**Parameters**
- `chapters`: chapters from `00_text.jsonl`, written by `lnvox ingest --mode lecture`.
- `output_dir`: `artifacts/<book_id>/`. `03_directed/` is created under it.
- `casting`: the `BookCasting` from `04_voice_assignments.json`, or `None` to use the default narrator.
- `client`: an `LLMClient`, or `None`. With `None` no LLM is called, whatever `normalize` is.
- `normalize`: run the speech-normalize pass. Defaults to `True`.
- `on_chapter_done`: optional callback. Unlike the other stages, it receives **only** the `ChapterDirected` result, not `(chapter, result)`.

**Writes**: `03_directed/<chapter_id>.json`.

:::note[Chapters written before the records]
An old chapter file has no record, so it is taken as matching the current settings. If you switched `--no-normalize` or re-cast the narrator since it was written, delete `03_directed/` once to rebuild. From then on the records catch the change.
:::

### `narrator_descriptor`

```python
def narrator_descriptor(casting: Optional[BookCasting]) -> str
```

Returns the `voice_descriptor` of the casting entry whose `character_name == "Narrator"`, if it is non-empty, and otherwise `DEFAULT_NARRATOR_DESCRIPTOR` (`"adult, British, female, warm clear voice, measured and engaging storyteller"`). Voice cast normally fills that field for the Narrator: with `descriptor_from_clip(clip)` for `--narrator-clip` or an auto-cast clip, and with the prior volume's stored descriptor on reuse. So the descriptor tracks the clip's gender, age and accent. The descriptor carries no performance cue. Lecture mode never adds "whispered"-style directions.

### `split_chapter`

```python
def split_chapter(chapter: Chapter) -> list[DirectedBeat]
```

For each paragraph in `split_paragraphs(chapter.text)` (blank-line split), it runs `_split_long_text(paragraph, MAX_MERGED_BEAT_CHARS)` and emits one beat per non-empty chunk:

| Field | Value |
| --- | --- |
| `type` | `"narration"` |
| `speaker` | `"Narrator"` |
| `text` | The chunk. Equal to `source_span` until normalize overwrites it |
| `source_span` | The chunk, verbatim: the sync anchor |
| `source_paragraph` | **0-indexed** position of the paragraph in `split_paragraphs(...)`, so Stage 6 can place code, table and figure elements after the right paragraph |
| `direction`, `prompt` | `""` at this point. Filled in by `direct_chapter` |

A paragraph that fits in 375 characters is one beat. Longer paragraphs are split at `.`/`!`/`?` followed by whitespace, never mid-sentence. Beats never span two paragraphs.

### `_normalize_beats`

```python
def _normalize_beats(client, beats: list[DirectedBeat]) -> None
```

Speech-normalizes `text` in place, in batches of `_NORMALIZE_BATCH` (25) beats. Each batch is rendered into `lecture_normalize.jinja` as lines `[i] <source_span>`, where `i` is the 0-based index **within the batch**. The call asks for `NormalizedBeats` (`[{index, text}]`) with `budget_for(desired=max(2048, int(total_span_chars / 2) + 1024), floor=2048)`.

The contract (DESIGN.md §13.6) is to **respell for the ear, never rewrite**. The model must:

- spell out numbers, dates and ordinals;
- verbalize units, currency, `%` and math symbols;
- expand `e.g.`, `Fig.`, `et al.` and the like;
- shorten or drop noisy URLs.

It must not paraphrase, add, drop, reorder, merge or split anything, and must add no performance direction. The prompt only ever shows the model `source_span`. The code only ever writes `text`.

Failure handling is best-effort for bad answers:

- an unusable answer for a batch (bad or unparseable JSON after the client's retries) skips that batch, and its beats keep `text == source_span`;
- a missing index, or an entry whose stripped `text` is empty, keeps that beat verbatim;
- indices outside the batch are ignored.

So a flaky normalize call degrades to literal reading and never drops a beat. A transport error (`is_transport_error`: server unreachable, timeout, 5xx) is **re-raised**: degrading would cache every remaining chapter un-normalized, and the step would never fail for the launcher's `ensure_vllm` retry to restart the server.

### `direct_chapter`

```python
def direct_chapter(
    chapter: Chapter, descriptor: str, *, client=None, normalize: bool = True
) -> ChapterDirected
```

Runs `split_chapter`. If `normalize` is true, `client` is set and there is at least one beat, it runs `_normalize_beats`. Then it sets every beat's `direction = descriptor` and `prompt = format_prompt(descriptor, beat.text)`. The prompt is built from the **spoken** text, after normalization. It returns one `DirectedScene`, `scene_id = "<chapter_id>_s1"` with an empty `location_hint`, inside a `ChapterDirected` with the chapter's id, `voice_descriptors = {"Narrator": descriptor}` and `normalized` = whether the normalize pass ran.

```python
lecture.direct_chapter(ch, "adult, British, male, clear voice", client=None, normalize=False)
# beat.prompt == '(adult - british - male - clear voice) "Hello world."'
```

## Output artifact

```json
{
  "chapter_id": "04",
  "voice_descriptors": {
    "Narrator": "adult, British, male, warm clear voice, measured and engaging storyteller"
  },
  "normalized": true,
  "scenes": [
    {
      "scene_id": "04_s1",
      "location_hint": "",
      "beats": [
        {
          "type": "narration",
          "text": "By nineteen seventy-three, ARPANET linked forty hosts (see Figure three).",
          "speaker": "Narrator",
          "direction": "adult, British, male, warm clear voice, measured and engaging storyteller",
          "prompt": "(adult - british - male - warm clear voice - measured and engaging storyteller) \"By nineteen seventy-three, ARPANET linked forty hosts (see Figure three).\"",
          "source_span": "By 1973, ARPANET linked 40 hosts (see Fig. 3).",
          "source_paragraph": 0,
          "emotion": "calm"
        }
      ]
    }
  ]
}
```

`text` (spoken) differs from `source_span` (verbatim) exactly where normalization fired. s6 anchors on `source_span`, so the synced EPUB still highlights the original characters.

## Notes

- **One scene per chapter.** DESIGN.md §13.3 allows "one `DirectedScene` per chapter, or per heading section". The code always emits one scene per chapter, so s5 puts only intra-scene pauses between a chapter's beats.
- **Normalize granularity.** It works in fixed batches of 25 beats, not the "per-paragraph-group" in DESIGN.md §13.3. A failed batch costs up to 25 beats of normalization.
- **Paragraph index base.** `source_paragraph` here is 0-indexed. s2's `start_paragraph`/`end_paragraph` are 1-indexed. Narration-mode directed beats carry `-1`.
- Design: DESIGN.md §13.3 (stage), §13.6 (normalize contract).
