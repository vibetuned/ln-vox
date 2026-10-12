---
title: Stage 0a — PDF extraction
description: lnvox ingest-pdf converts a PDF into the novels/<book_id>/ folder Stage 0 reads — chapters by page range or bookmarks, page furniture stripped, paragraphs rebuilt — plus the cover for the m4b and an EPUB that Stage 6 syncs with it.
sidebar:
  order: 1
  label: 0a · ingest-pdf
---

`lnvox ingest-pdf` turns a PDF into the same layout [`ingest-epub`](/stages/ingest-epub/) produces, so every later stage treats the book like an EPUB import:

- one `NN-<slug>.txt` per chapter, the title on the first line;
- `images/cover.jpg`, the cover page's image — Stage 5 embeds it in the m4b;
- `.epub_meta.json`, the same sidecar `ingest-epub` writes (title, authors, cover, chapter → page and EPUB-spine map);
- **an EPUB built from the cleaned chapters**, `epubs/<book_id>.epub` by default — the sync target for [Stage 6](/stages/s6-sync/). Its text *is* the chapters' text, so the beats align onto it almost exactly.

It reads only the PDF's **text layer**. A scan without OCR has none — run it through OCR first (e.g. `ocrmypdf in.pdf out.pdf`); a scanner's own OCR layer, like Acrobat Paper Capture, works as is. The launcher never runs it: extract once, then run the launcher (or [`lnvox ingest`](/stages/ingest/)) on the folder.

```bash
uv run --with "pypdf[image]" lnvox ingest-pdf book.pdf novels/open-mind/volume-01 \
    --chapter "5-12:Atomic Explosives" \
    --chapter "13-20:Atomic Weapons and American Policy" \
    --title "The Open Mind" --author "J. Robert Oppenheimer"

./scripts/run_pipeline.sh open-mind/volume-01 --mode lecture --book-title "The Open Mind"
```

The launcher finds `epubs/open-mind/volume-01.epub` on its own, so the run ends with the m4b (cover embedded) **and** `07_sync/` — the synced EPUB plus `sync_manifest.json` for the reader.

pypdf and Pillow (the `image` extra, for the cover) aren't locked dependencies — adding one re-resolves the whole lock, including the vLLM nightly — so the command runs them as an overlay with `uv run --with`. Without pypdf it exits 2 with that hint; without Pillow it skips the cover and says so.

## Options

| Option | Default | Effect |
| --- | --- | --- |
| `--chapter FIRST-LAST:Title` | — | One chapter: 1-based, inclusive pages; the title is optional (`Chapter N` without one) and may contain `:`. Repeat per chapter. Pages outside every range are skipped, which is how front matter, a table of contents or a jacket blurb stay out. |
| `--pages FIRST-LAST` | all pages | Without `--chapter`: read only these pages. Bookmarked chapters are clipped to the range. |
| `--title`, `--author` | the PDF's metadata | Book title / author(s) for the EPUB and the sidecar. Scans rarely carry metadata, so pass them. `--author` repeats. Fallback title: the PDF's file name. |
| `--language` | `en` | The EPUB's language code. |
| `--cover-page N` | `1` | The page whose largest image is the cover; `0` for none. |
| `--cover IMAGE` | — | Use this image file as the cover instead (overrides `--cover-page`). A JPEG within the 1600 px cap is copied byte for byte; anything else is converted like a page's image. Pointing it at the folder's own `images/cover.jpg` keeps the current cover across a re-extraction. |
| `--epub PATH` / `--no-epub` | `epubs/<book_id>.epub` | Where to write the sync EPUB, or don't. `<book_id>` is derived from the output folder as `lnvox ingest` derives it (`novels/a/b` → `a/b`). An existing EPUB is overwritten only if ln-vox generated it — your own EPUB at that path makes the command exit 2 untouched. |

Without `--chapter`, the PDF's **top-level bookmarks** become chapters, each running to the page before the next one (nested bookmarks are ignored). Without bookmarks the whole document — or `--pages` — is one chapter, named after the title.

## Cleanup

Each chapter's pages are cleaned of what a narrator must not read, then re-flowed:

1. **Page edges.** Lines that recur at the top or bottom two lines of a page — on at least 2 pages and 20% of the chapter's pages — are running heads or feet and are dropped. Matching ignores digits and case, so `12 THE OPEN MIND` and `The Open Mind 13` are one head; detection is per chapter, because a book's recto head is usually the chapter title. Page numbers, lone roman numerals and scan specks (no two letters in a row) are dropped from the edges too.
2. **The chapter title** printed at the top of its first page is dropped — fuzzy-matched, so an OCR'd `Atomie Explosives` still matches `Atomic Explosives`, and it may wrap over several lines. A bookmark title's leading numbering and trailing parenthetical are optional in the match: `I. Atomic Explosives (May 1946)` matches a page headed just `Atomic Explosives`.
3. **Line breaks → paragraphs.** Words split across lines are re-joined: a soft hyphen always, a plain `-` when the next line starts lowercase. A line that ends a sentence and is shorter than 85% of the median line ends a paragraph. Ligatures (`ﬁ`, `ﬂ`, …) become letters; rule-line debris glued to a line start (`------ - ---there`) is removed.
4. **Missing pages.** When the printed page numbers jump (63 → 66), a paragraph break keeps the sentence fragments on either side apart instead of gluing them into one.

Pages with no text are listed in the output. A PDF with no text at all is refused (exit 2) before anything is written.

## Cover and EPUB

The cover is the **largest raster image** on `--cover-page` (masks and specks under 200 px don't count): a scan's page image, or a publisher PDF's cover art. It is saved as `images/cover.jpg` with its long side capped at 1600 px, recorded as `cover_image` in the sidecar — which `lnvox ingest` copies into `00_book_meta.json` for Stage 5 — and used as the EPUB's cover. A purely vector cover would need a PDF rasterizer and is skipped with a message.

The EPUB is EPUB 3 with an EPUB 2 NCX: a cover page (outside the reading order), then one XHTML per chapter, `chapterNN.xhtml`, holding an `<h1>` title and one `<p>` per paragraph — exactly what `ingest-epub` parses, so reading it back yields the same chapter files byte for byte. The sidecar's `source_parts` name those spine items, which is how Stage 6 finds each chapter's text.

Validated on a scanned 21-page excerpt: two chapters, the cover embedded in the m4b, and Stage 6 matched 91/91 beats with the timeline ending on the m4b's last sample.

## Re-running

A re-extraction into the same folder first removes the chapter files the previous run listed in `.epub_meta.json` — so changing the split never leaves stale chapters for Stage 0 to ingest twice — and regenerates the EPUB. Files the extractor didn't write are left alone.

The [lecture stage](/stages/lecture/) notices a chapter whose text changed and directs it again, so re-extracting into an existing book needs no cleanup. Previously rendered audio is reused for every beat whose final prompt comes out identical (the TTS cache is content-keyed); text from a different scan or edition rarely does.

To keep the book's current cover when re-extracting — say from a better-OCR'd PDF whose first page is a poorer cover — pass it back with `--cover novels/<book>/images/cover.jpg`.

## Limits

- Layout is not analysed: two-column pages, footnotes and tables come out in pypdf's reading order. Footnotes in particular end up inline; trim them by hand if they matter.
- A line hyphen before an uppercase word (`Anglo-` / `Saxon`) is kept; one before a lowercase word is treated as a line-break hyphen, so a genuine compound split exactly there loses its hyphen (`well-` / `known` → `wellknown`).
- OCR errors pass through, into the EPUB too. Lecture mode's speech-normalize pass smooths some for the audio; for a book you care about, proofread the `.txt` files before the first render. Stage 6's fuzzy and anchored matching tolerates small differences between them and the EPUB, but a re-extraction would overwrite your fixes.
- The EPUB carries text and the cover only — no figures from inside the PDF.
