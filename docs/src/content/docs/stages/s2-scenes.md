---
title: Stage 2 — scene segmentation
description: lnvox s2 splits each chapter into scenes and speaker-tagged narration/dialogue beats with verbatim source_span, cached per chapter in 02_scenes/.
sidebar:
  order: 5
  label: 2 · scenes
---

Stage 2 writes `02_scenes/<chapter_id>.json`. Each chapter is split into scenes, given as chapter-global paragraph ranges, and each scene into ordered **beats**: `narration` or `dialogue`, with a speaker from the cast, the spoken `text`, and a verbatim `source_span`. Almost everything downstream depends on it. s3 directs these beats. The [casting audit](/stages/voice-audit/) reads scene co-occurrence from them. [s6](/stages/s6-sync/) anchors highlights on `source_span`. It runs only in narration mode.

## Command

```bash
uv run lnvox s2 <book_id> [--chapters 01,02]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `BOOK_ID` (argument) | required | Book id. Reads and writes `artifacts/<book_id>/` |
| `--chapters` | all chapters | Comma-separated chapter ids to segment |

The command exits 1 if `00_text.jsonl` or `01_characters.json` is missing, or the filter matches nothing. Per chapter it prints `✓ <id>: N scene(s), X narration / Y dialogue`, and at the end a summary table that includes every dialogue speaker seen. That table is the quickest check for misattributed or `Unknown` speakers.

## In the launcher

```bash title="scripts/run_pipeline.sh"
        banner "Stage 2: scene segmentation"
        run_step ensure_vllm "Stage 2 (scene segmentation)" uv run lnvox s2 "$BOOK_ID"
```

- It runs in narration mode only, right after s1. It is skipped for lecture and scenario mode and with `--skip-llm`.
- `run_step ensure_vllm` gives the step up to `--step-retries` attempts (default 3, `$STEP_RETRIES`), `$RETRY_DELAY` seconds apart (default 5). Before each retry, `ensure_vllm` relaunches the managed server if it is dead. A signal exit (Ctrl-C) aborts immediately, and so does exit 2 (a deterministic refusal). s2 is built for this: boundary and beat calls let exceptions propagate instead of caching an empty result, and finished chapters are reloaded from `02_scenes/`.

## Inputs and outputs

| Path under `artifacts/<book_id>/` | Direction | Contents |
| --- | --- | --- |
| `00_text.jsonl` | read | Chapter text |
| `01_characters.json` | read | Cast. Only `name` and `aliases` are sent to the model |
| `02_scenes/<chapter_id>.json` | write (cache) | `ChapterScenes`: scenes with `start_paragraph`, `end_paragraph`, `location_hint` and `beats` |

There is a full example on the [library page](/library/stages/s2-scenes/#output-artifact).

## How it works

`cli.stage2` → [`s2_scenes.run`](/library/stages/s2-scenes/#run) → [`segment_chapter`](/library/stages/s2-scenes/#segment_chapter) → [`chunk_text`](/library/llm/chunker/) → [`_detect_boundaries`](/library/stages/s2-scenes/#_detect_boundaries) (pass 2a, per chunk) → [`_tag_scene_beats`](/library/stages/s2-scenes/#_tag_scene_beats) (pass 2b, per scene) → `_normalize_speakers`.

1. **Paragraphs.** The chapter text is split on blank lines, once. Paragraph numbers are 1-indexed positions in that list, for the whole chapter.
2. **Chunks.** Chapters over 30 000 characters are cut into paragraph-aligned chunks. At each cut the LLM picks the cleanest break among 6 candidate paragraphs, falling back to the middle one on error.
3. **Pass 2a: boundaries.** Per chunk, the model sees locally numbered paragraphs `[1]…[N]` and returns scene ranges. The code clamps them into `1..N` and offsets them by the chunk's first paragraph, which makes them chapter-global. An empty answer makes the whole chunk one scene. The chapter's ranges are then repaired to tile every paragraph exactly once: gaps are folded into the earlier scene, overlaps trimmed, and the head and tail covered. Each repair is logged as `[s2] <chapter>: scene ranges — <note>`.
4. **Pass 2b: beats.** Per scene, only that scene's paragraphs go to the model. It returns beats in which dialogue `text` has quotes and "she said" tags removed, while `source_span` copies the exact source slice, tags and quote marks included. Scene ids are renumbered `<chapter_id>_s1…sN`. A non-empty scene that comes back with zero beats raises, so the retry can restart the server.
5. **Speaker cleanup.** Dialogue speakers that exactly (case-insensitively) match a cast alias are rewritten to the canonical name. s3 later does the fuzzier folding (leading articles, `the narrator`).

## Debugging bad segmentation

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| A passage is missing from the audio or read twice | A chapter segmented before ranges were repaired, with a gap or overlap between scene ranges. New runs tile every chapter | Run the range check below, delete that chapter's `02_scenes` file, and re-run |
| `Unknown` or odd speaker names in the summary table | The model couldn't attribute the line, or used a spelling not in the cast | Article and alias differences are fixed in s3. Truly unknown speakers render without a cast voice |
| "he said" read in a character's voice | The model left the attribution tag inside dialogue `text` | Check the beat's `text` against its `source_span`. Re-segment the chapter |
| Low exact-match rate in s6 | `source_span` drift: the model paraphrased the span instead of copying it | Re-segment the worst chapters, or try a stronger model |
| `s2: scene … returned 0 beats … likely a stalled LLM` | The server stopped producing output mid-chapter | The launcher retries automatically. By hand, restart the server and re-run |

### Checking scene ranges

Scenes tile every chapter: the first starts at 1, each starts right after the previous one ends, and the last ends on the final paragraph. s2 repairs the model's ranges to guarantee it, but chapters cached by an older version may not. The following script reports the chapters where they don't:

```python
# uv run python check_ranges.py
from pathlib import Path

from lnvox.ingest.text import read_jsonl
from lnvox.llm.chunker import split_paragraphs
from lnvox.llm.schemas import ChapterScenes

book = Path("artifacts/my-series/volume-01")
chapters = {c.chapter_id: c for c in read_jsonl(book / "00_text.jsonl")}
for f in sorted((book / "02_scenes").glob("*.json")):
    cs = ChapterScenes.model_validate_json(f.read_text(encoding="utf-8"))
    n = len(split_paragraphs(chapters[cs.chapter_id].text))
    expected = 1
    for s in cs.scenes:
        if s.start_paragraph != expected:
            print(f"{s.scene_id}: starts at {s.start_paragraph}, expected {expected}")
        expected = s.end_paragraph + 1
    if expected != n + 1:
        print(f"{cs.chapter_id}: last scene ends at {expected - 1} of {n} paragraphs")
```

To see what paragraph `k` of a chapter is, print `split_paragraphs(chapter.text)[k - 1]`.

## Re-running and caching

- A chapter with a parseable `02_scenes/<chapter_id>.json` is **not** re-segmented. Delete the file to redo that chapter.
- A cached file with no scenes, or with any scene that has zero beats, is treated as broken, a leftover of an old silent-failure bug. It is re-segmented automatically.
- The cache doesn't depend on the cast. After changing `01_characters.json`, stale speaker tags remain until you delete the chapter files. s3's speaker canonicalisation hides article and alias mismatches, but not renames.
- s3's own cache (`03_directed/`) notices a re-segmented chapter only if its set of speakers changed. Delete the matching `03_directed/<chapter_id>.json` too.

## Failure modes

- **Dead or crashing server.** 2a and 2b exceptions propagate and the step fails, so `run_step` can relaunch the server and retry. Chunk-break selection is the only call that swallows errors.
- **Truncation on very long scenes.** Beat budgets are `max(4096, min(32000, 2 × scene_chars + 1024))` tokens, clamped to what the context leaves after the prompt. The client grows a cut-off budget by 1.5 times up to the context ceiling. A single enormous scene can still fail. Look for `finish_reason=length` in the error diagnostics.
- **Smaller server context.** Boundary and beat budgets are clamped to the client's context window: `LNVOX_LLM__MAX_MODEL_LEN` / `LNVOX_LLM_MAX_MODEL_LEN`, falling back to `LNVOX_LLM_MAX_LEN`, default 65 536. The launcher's `--max-model-len` exports all three, and a launcher-started `serve_vllm.sh` passes its 20 480 default for 27B/31B models on to the client. As with s1, only a window chosen inside a serve script you started yourself, or on an external `--vllm-url` server, stays unknown to the client: export `LNVOX_LLM_MAX_LEN` for both, or pass `--max-model-len`. See [context length](/reference/environment/#llm-context-length-three-names).

Design rationale: DESIGN.md §2.3.
