---
title: Stage 0a — PDF extraction
description: lnvox ingest-pdf converts a PDF into the novels/<book_id>/ folder Stage 0 reads — chapters by page range or bookmarks, page furniture stripped, paragraphs rebuilt — plus the cover for the m4b and an EPUB that Stage 6 syncs with it; from a born-digital PDF also its figures, tables and endnotes, for the reader app.
sidebar:
  order: 1
  label: 0a · ingest-pdf
---

`lnvox ingest-pdf` turns a PDF into the same layout [`ingest-epub`](/stages/ingest-epub/) produces, so every later stage treats the book like an EPUB import:

- one `NN-<slug>.txt` per chapter, the title on the first line;
- `images/cover.jpg`, the cover page — Stage 5 embeds it in the m4b;
- with `--font-filter`, `images/figure-2-1.png`, `images/table-9-3.png`, … and `images/note-04-12.png`: the book's figures, tables and endnotes, rendered from the page (see [Figures, tables and notes](#figures-tables-and-notes));
- `.epub_meta.json`, the same sidecar `ingest-epub` writes (title, authors, cover, chapter → page and EPUB-spine map);
- **an EPUB built from the cleaned chapters**, `epubs/<book_id>.epub` by default — the sync target for [Stage 6](/stages/s6-sync/). Its text *is* the chapters' text, so the beats align onto it almost exactly.

It reads only the PDF's **text layer**. A scan without OCR has none — run it through OCR first (e.g. `ocrmypdf in.pdf out.pdf`); a scanner's own OCR layer, like Acrobat Paper Capture, works as is. The launcher never runs it: extract once, then run the launcher (or [`lnvox ingest`](/stages/ingest/)) on the folder.

```bash
uv run --with "pypdf[image]" lnvox ingest-pdf book.pdf novels/my-essays/volume-01 \
    --chapter "5-12:The First Lecture" \
    --chapter "13-20:The Second Lecture" \
    --title "Collected Lectures" --author "A. Author"

./scripts/run_pipeline.sh my-essays/volume-01 --mode lecture --book-title "Collected Lectures"
```

The launcher finds `epubs/my-essays/volume-01.epub` on its own, so the run ends with the m4b (cover embedded) **and** `07_sync/` — the synced EPUB plus `sync_manifest.json` for the reader.

pypdf and Pillow (the `image` extra, for the cover) aren't locked dependencies — adding one re-resolves the whole lock, including the vLLM nightly — so the command runs them as an overlay with `uv run --with`. Without pypdf it exits 2 with that hint; without Pillow it skips the cover and says so. Rendering — the whole cover page, figures, notes — also needs **pypdfium2**, another overlay: `uv run --with "pypdf[image]" --with pypdfium2 lnvox ingest-pdf …`. Without it the cover falls back to the page's largest image, and figures and note images are skipped with a message.

## Options

| Option | Default | Effect |
| --- | --- | --- |
| `--chapter FIRST-LAST:Title` | — | One chapter: 1-based, inclusive pages; the title is optional (`Chapter N` without one) and may contain `:`. Repeat per chapter. Pages outside every range are skipped, which is how front matter, a table of contents or a jacket blurb stay out. |
| `--pages FIRST-LAST` | all pages | Without `--chapter`: read only these pages. Bookmarked chapters are clipped to the range. |
| `--title`, `--author` | the PDF's metadata | Book title / author(s) for the EPUB and the sidecar. Scans rarely carry metadata, so pass them. `--author` repeats. Fallback title: the PDF's file name. |
| `--language` | `en` | The EPUB's language code. |
| `--cover-page N` | `1` | The cover page, rendered whole (with pypdfium2; else its largest image); `0` for none. |
| `--cover IMAGE` | — | Use this image file as the cover instead (overrides `--cover-page`). A JPEG within the 1600 px cap is copied byte for byte; anything else is converted like a page's image. Pointing it at the folder's own `images/cover.jpg` keeps the current cover across a re-extraction. |
| `--epub PATH` / `--no-epub` | `epubs/<book_id>.epub` | Where to write the sync EPUB, or don't. `<book_id>` is derived from the output folder as `lnvox ingest` derives it (`novels/a/b` → `a/b`). An existing EPUB is overwritten only if ln-vox generated it — your own EPUB at that path makes the command exit 2 untouched. |
| `--font-filter` | off | For a born-digital PDF: keep only the running text, told apart by its font, and take out its figures, tables and endnotes for the reader app. See [Born-digital PDFs](#born-digital-pdfs---font-filter). |
| `--notes FIRST-LAST` | the `Notes` bookmark | With `--font-filter`: the pages of the endnotes section. They are never narrated. |
| `--no-notes` / `--no-figures` | — | With `--font-filter`: leave the endnotes out (markers are still removed) / drop figures and tables without rendering them. |
| `--drop-pattern REGEX` | — | Remove every match of this regular expression from every paragraph: book-specific furniture such as margin references to another edition's pages (`p\. [A-Z]\d+`). Repeat for several. |
| `--rewrite 'REGEX=>TEXT'` | — | Replace every match in every paragraph (`re.sub`, so `\1` and `\n` work in `TEXT`). A blank line (`\n\n`) in the result starts a new paragraph, and a paragraph left empty is dropped. Repeat for several; they run in order, after `--drop-pattern`. See [Book-specific cleanup](#book-specific-cleanup). |

Without `--chapter`, the PDF's **top-level bookmarks** become chapters, each running to the page before the next one (nested bookmarks are ignored). Without bookmarks the whole document — or `--pages` — is one chapter, named after the title.

## Cleanup

Each chapter's pages are cleaned of what a narrator must not read, then re-flowed:

1. **Page edges.** Lines that recur at the top or bottom two lines of a page — on at least 2 pages and 20% of the chapter's pages — are running heads or feet and are dropped. Matching ignores digits and case, so `12 COLLECTED LECTURES` and `Collected Lectures 13` are one head; detection is per chapter, because a book's recto head is usually the chapter title. Page numbers, lone roman numerals and scan specks (no two letters in a row) are dropped from the edges too.
2. **The chapter title** printed at the top of its first page is dropped — fuzzy-matched, so an OCR'd `The Flrst Lecture` still matches `The First Lecture`, and it may wrap over several lines. A bookmark title's leading numbering and trailing parenthetical are optional in the match: `I. The First Lecture (May 1950)` matches a page headed just `The First Lecture`.
3. **Line breaks → paragraphs.** Words split across lines are re-joined: a soft hyphen always, a plain `-` when the next line starts lowercase. A line that ends a sentence and is shorter than 85% of the median line ends a paragraph. Ligatures (`ﬁ`, `ﬂ`, …) become letters; rule-line debris glued to a line start (`------ - ---there`) is removed.
4. **Missing pages.** When the printed page numbers jump (63 → 66), a paragraph break keeps the sentence fragments on either side apart instead of gluing them into one.
5. **Tables of contents.** A paragraph of contents entries (dot leaders ending in a page number: two or more, or one in a short paragraph) is dropped, with a `Contents` / `Table des matières` / `Sommaire` heading right before it. LaTeX books print such a mini contents at the head of every chapter, in the body font.
6. **Typography.** LaTeX's ellipsis, which extracts as three spaced periods (`. . .`), becomes `…`. Control characters (a maths glyph with no Unicode mapping extracts as NUL, which the EPUB's XML can't carry) are removed.

Pages with no text are listed in the output.

### Book-specific cleanup

Some furniture belongs to one edition only, so no default rule should guess at it. `--drop-pattern` and `--rewrite` apply your own regular expressions to the final paragraphs. They run after note markers are resolved, so a marker no note claims (put back as text) is covered too, and they change the chapter files and the EPUB alike, which keeps Stage 6 aligned. Examples (invented):

| Furniture | Option |
| --- | --- |
| Margin references to the typescript, `… the gate p. K4 stood …` | `--drop-pattern '\bp\.\s?[A-Z]\s?\d+\b'` |
| Page footnotes kept in the body: a body `12(*) Or so …` after a sentence becomes its own paragraph | `--rewrite '(?:^\|(?<=[.!?] ))\d{1,4}\(\*+\)\s*(?=[A-Z])=>\n\nNote: '` |
| …then the calls `(*)` left in the running text go | `--rewrite '\s?\(\*+\)=>'` |
| Layout debris as whole paragraphs (`⋄` separators, shredded display maths): 12 characters or fewer, no word of 5 letters | `--rewrite '^(?!.*[^\W\d_]{5}).{0,12}$=>'` |

Check the result with the counts you care about before rendering. A rule that drops short paragraphs is safe in a monograph and wrong in a novel, whose `Oui.` is dialogue. A PDF with no text at all is refused (exit 2) before anything is written.

## Born-digital PDFs (`--font-filter`)

A publisher's digital edition has a real font on every glyph, which tells the running text apart from everything a narrator must skip. `--font-filter` reads each text run's font, size and position (pypdf's `visitor_text`) instead of the page's plain text:

- **The body font** is the family and size carrying the most characters across the extracted pages. The output names it — `font filter: body text is SourceSerif 9 pt (94% of the characters)` — and warns when no font reaches half the text, which usually means a scan's OCR layer: don't use the filter there.
- **Taken out of the text:** figures and tables — from the text before a `Figure 2.1` / `Table 3` / `Example 4` label (axis labels, cells set ahead of it) through its caption until the running text resumes — and running heads, folios, figure lettering, and any other text in another family at another size. Figures and tables come back as pictures; see below.
- **Note markers** are taken out: a superscript number set smaller, and the more common kind set at body size and only raised, which pypdf can't see — digits right after a sentence ends (`the gate. 4 Nobody`, `the night.4 Most`, `“…”13 Then`). The sentence must end with a lowercase word, a closing quote or a bracket, so `Op. 27 No. 2`, `p. 12` and pitch names like `C4` are left alone. Once the notes section is read, every marker must be claimed by a note — see below.
- **Kept:** the body font in any style (italic, bold); text inline on a body line (a `#` from a symbol font, a term set in the sans); the body family at a smaller size — block quotes, epigraphs.
- **Section headings** — a line in another family at body size or larger, on a page with running text — become a paragraph of their own; a heading set in two styles or wrapped over two lines is rejoined. Display lines on a page with no running text (a half-title) are dropped.
- **Paragraphs** start where a line is indented from its page's margin, where the text switches between body and block-quote size, and at a numbered item (`3. `) after a sentence end — on top of the line-length rule above, which misses a paragraph whose last line runs nearly full.

The other cleanup steps (running heads by repetition, the chapter title, hyphenation) run as usual on the filtered text.

## Figures, tables and notes

With `--font-filter`, what the text loses comes back for the reader — in the EPUB, in the m4b and, after Stage 6, in `sync_manifest.json` at its playback time. The progress log sums it up:

```text
  notes: 212 in 12 group(s), pages 301-318
  figures: 64 rendered — 61 placed before their first mention, 3 where printed
  notes: 212 of 212 tied to their markers; 1 marker-like number(s) left in the text
```

**Figures and tables** are rendered from the page at 216 dpi and trimmed to their ink, as `images/figure-2-1.png`, `images/table-3-2a.png`, … The crop follows the caption. A figure's caption sits under its drawing, so the picture runs from the nearest text above — running text, another figure, the running head — down to the caption's last line; a table's caption heads it, so the picture runs from the label down to the next text. On a page with several figures, lettering and cells go to the caption on their drawing's side, whatever order the PDF lists them in, and a table continued over a page break becomes one picture. Each is placed **before the paragraph that first mentions it** (`Figure 2.1 provides…`, `figures 4.1 and 4.2`, `Tables 3.2a–3.2b`), so the reader shows it as the narrator gets to it — else after the paragraph it interrupted in print. In the EPUB it is a `<figure>` with the picture and its label and caption as `alt` text. The sidecar's `images` lists it after the cover, so Stage 5 embeds it in the m4b and Stage 6 times it like any illustration (`images[]` and `visual_elements[]` of kind `image`, with `alt`).

**Endnotes** are read from the notes section — the `Notes` bookmark, or `--notes` — whose group headers (`Preface`, `Chapter 4`) pair with the chapters (`4 The Middle Years`). A note runs from its `N.` at the margin over indented paragraphs and page breaks; a misnumbered note set off by the space between notes stays a note of its own. Each marker in the text must be claimed by its chapter's note of that number; a marker no note claims was a number after all and goes back into the text (`the quartet opus 12, no. 2 (in G)`). A note still unclaimed is looked for between its neighbours' markers, in the forms the first pass skips — after a capital (`the letter A.1 Others`), a number (`table 3.1. 5 In`), a comma (`as studies show,13 memory`). Every note becomes:

- a `footnote` visual element in the sidecar: its rendered image (`src`), its text as `html` and plain `text`, its number (`label`) and an `anchor` — the words just before its marker. [Stage 6](/stages/s6-sync/) places it right after the beat that reads those words, so the reader app shows the note as that sentence ends;
- in the EPUB, an empty `noteref` link where the marker stood — a stylesheet draws the number, so the paragraph's text runs on unbroken for the sync — and an endnote at the chapter's end that links back.

Validated on a born-digital academic book of nearly 500 pages: over a hundred figures and tables rendered, every one of its ~400 endnotes tied to its marker. Against an unfiltered extraction the text lost only figures, tables, markers and running heads — no prose. A simulated render (silent audio through the real Stage 5 and Stage 6) matched every beat `span-exact`, embedded the cover and all the figures in the m4b, and timed every picture and note in reading order.

## Cover and EPUB

The cover is `--cover-page` **rendered whole** with pypdfium2, long side 1600 px: a publisher's cover is often layered — artwork in pieces, vector shapes, the title as text — and its largest image alone is a fragment. Without pypdfium2 it is the page's **largest raster image** (masks and specks under 200 px don't count), fine for a scan; a purely vector cover is then skipped with a message. It is saved as `images/cover.jpg`, recorded as `cover_image` in the sidecar — which `lnvox ingest` copies into `00_book_meta.json` for Stage 5 — and used as the EPUB's cover.

The EPUB is EPUB 3 with an EPUB 2 NCX: a cover page (outside the reading order), then one XHTML per chapter, `chapterNN.xhtml`, holding an `<h1>` title and one `<p>` per paragraph — exactly what `ingest-epub` parses, so reading it back yields the same chapter files byte for byte. Figures (`<figure>`) and endnotes (`<aside>` holding `<div>`s) add no `<p>`, so that holds with them too. The sidecar's `source_parts` name those spine items, which is how Stage 6 finds each chapter's text.

Validated on a scanned 21-page excerpt: two chapters, the cover embedded in the m4b, and Stage 6 matched 91/91 beats with the timeline ending on the m4b's last sample.

## Re-running

A re-extraction into the same folder first removes the chapter files and the figure and note images (`rendered`) the previous run listed in `.epub_meta.json` — so changing the split never leaves stale chapters for Stage 0 to ingest twice — and regenerates the EPUB. Files the extractor didn't write are left alone.

The [lecture stage](/stages/lecture/) notices a chapter whose text changed and directs it again, so re-extracting into an existing book needs no cleanup. Previously rendered audio is reused for every beat whose final prompt comes out identical (the TTS cache is content-keyed); text from a different scan or edition rarely does.

To keep the book's current cover when re-extracting — say from a better-OCR'd PDF whose first page is a poorer cover — pass it back with `--cover novels/<book>/images/cover.jpg`.

## Limits

- Layout is not analysed: two-column pages, footnotes and tables come out in pypdf's reading order. Footnotes in particular end up inline; trim them by hand if they matter. `--font-filter` handles the figures, tables and endnotes of a born-digital PDF, but keeps footnotes at a page's foot set in the body family (it can't tell them from a block quote), and a table without a `Table N` label passes through. A figure is cropped full width between the text around it, so a figure set beside the running text would take that text along.
- A note whose marker sits inside a figure's caption has no place in the running text; it is not tied, and the log's `N of M tied` says so.
- A line hyphen before an uppercase word (`Anglo-` / `Saxon`) is kept; one before a lowercase word is treated as a line-break hyphen, so a genuine compound split exactly there loses its hyphen (`well-` / `known` → `wellknown`).
- OCR errors pass through, into the EPUB too. Lecture mode's speech-normalize pass smooths some for the audio; for a book you care about, proofread the `.txt` files before the first render. Stage 6's fuzzy and anchored matching tolerates small differences between them and the EPUB, but a re-extraction would overwrite your fixes.
- Without `--font-filter` (a scan), the EPUB carries text and the cover only — no figures, no notes.
