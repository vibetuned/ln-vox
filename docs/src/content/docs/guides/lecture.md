---
title: Lecture mode (non-fiction)
description: One steady narrator reading a technical or non-fiction book as written, with code, tables and figures shown as images instead of read aloud.
sidebar:
  order: 2
---

Lecture mode is the opposite of narration: no characters, no scenes, no dramatization. A single narrator reads the text *as written*, speech-normalized for the ear, and everything that can't be read aloud — code, tables, figures — is rendered to an image that the reader app shows at the right moment. Design: DESIGN.md §13.

## What differs from narration

- **s1 + s2 + s3 collapse into one stage**, [`lnvox lecture`](/stages/lecture/): prose is split into TTS-sized narration beats, and each beat's `text` is speech-normalized (`1973` → "nineteen seventy-three", `Fig. 3` → "Figure three", `42%` → "forty-two percent") while `source_span` stays verbatim — so Stage 6 sync is near-perfect.
- **Ingest classifies blocks.** [`ingest-epub --mode lecture`](/stages/ingest-epub/) tags every block (prose, heading, code, table, figure, boilerplate…), drops TOC / copyright / index pages, and records code, tables and figures as *visual elements*, rendered to PNG (code syntax-highlighted) when the `render` extra is installed.
- **Casting is Narrator-only.** `lnvox ingest --mode lecture` writes an empty cast stub so [voice cast](/stages/voice-cast/) casts just the Narrator. Pick it by ear — it is 100% of the audio.

## Run

```bash
# One-time: the renderer for code/table images (optional; without it they're HTML-only).
uv sync --extra render && uv run playwright install chromium

uv run lnvox ingest-epub epubs/nonfiction/book.epub novels/nonfiction/book --mode lecture
./scripts/run_pipeline.sh nonfiction/book --mode lecture \
    --narrator-clip cv_051e865815e5 --book-title "Book Title — Subtitle"
```

The launcher runs ingest → voice cast → audit → lecture → s4 → s5 → s6.

## Flags that matter

| Flag | Effect |
| --- | --- |
| `--mode lecture` | Required. Routes ingest → voice cast → `lnvox lecture`, skipping s1/s2/s3. |
| `--no-normalize` | Read the text byte-literal: no speech-normalize LLM pass. |
| `--no-normalize` **and** `--narrator-clip` | Zero LLM calls: the launcher never starts an LLM server. |
| `ingest-epub --ingest-classifier none` | Rules-only block classification (no LLM during ingest). The default `fallback` consults the LLM only for untagged or ambiguous blocks — so it needs a server running. |
| `ingest-epub --no-render` | Record code/tables as HTML-only visual elements. |

## What the reader gets

`07_sync/sync_manifest.json` carries a `visual_elements` list next to the beats: each rendered code block, table or figure with the `trigger_seconds` at which the reader should flip to it — the same mechanism as novel illustrations. See [Stage 6](/stages/s6-sync/) and [`lnvox.ingest.render`](/library/ingest/render/).

:::caution[Limitations the design accepts (DESIGN.md §13.8)]
- Normalization is best-effort: LaTeX-dense math, chemistry and dense inline tabular prose read imperfectly (the worst case is the verbatim text, never a dropped beat).
- Structure comes from the EPUB markup: `.txt` input is all prose, and a caption the EPUB inlined as a plain `<p>` is narrated.
- One narrator for the whole book.
- A code block or table is **silent** in the audio — without the companion reader open, the listener simply hears the next prose beat.
:::
