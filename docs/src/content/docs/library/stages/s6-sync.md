---
title: lnvox.stages.s6_sync
description: Stage 6 library API — re-align directed beats onto the source EPUB, wrap them in spans, place images, and write sync_manifest.json.
sidebar:
  order: 9
  label: s6_sync
---

`lnvox.stages.s6_sync` re-aligns the Stage-3 beats onto the original EPUB's XHTML. It writes a copy of the EPUB with every matched beat wrapped in `<span class="lnvox-beat" data-beat-id="…">`, and a `sync_manifest.json` that gives each beat's start and end in the final m4b. A reader app such as ln-reader uses the two together to highlight text in time with playback and to show illustrations at the right moment. It is called by `lnvox s6` ([stage page](/stages/s6-sync/)). Narration and lecture books use it. Scenario mode uses [`scenario_sync`](/library/stages/scenario-sync/) instead.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`run`](#run) | function | Entry point: match, wrap, place images, write the three outputs. |
| [`align_images_to_embeds`](#align_images_to_embeds) | function | One manifest `images` entry per embedded m4b picture, in embed order. |
| [`place_visual_element`](#place_visual_element) | function | Lecture mode: map a block's `after_paragraph` to neighbouring beats. |
| `SPAN_CLASS`, `SPAN_ATTR` | constants | `"lnvox-beat"`, `"data-beat-id"`. |
| [Tuning constants](#tuning-constants) | constants | Anchor lengths, forward-jump cap, fuzzy thresholds, Pass-2 slack, span-coverage floor, mix-drift tolerance, default pads. |
| [`_normalize_with_map`](#_normalize_with_map), [`_build_shadow`](#_build_shadow) | private | Shadow string and DOM map. |
| [`_find_source_span`](#_find_source_span), [`_find_beat`](#_find_beat), [`_fuzzy_match`](#_fuzzy_match), [`_find_beat_in_range`](#_find_beat_in_range) | private | The matcher ladder. |
| [`_wrap_matches`](#_wrap_matches) | private | DOM wrapping. |
| [`_image_owners`](#_image_owners), [`_write_modified_epub`](#_write_modified_epub), [`_inject_responsive_css`](#_inject_responsive_css), [`_count_beat_spans`](#_count_beat_spans), [`_compute_beat_timings`](#_compute_beat_timings), `_parse_opf` | private | Image ownership, EPUB repack, verification, timing. |

## API

### `run`

```python
def run(
    book_id: str,
    book_dir: Path,
    epub_path: Path,
    novel_dir: Path,
    output_dir: Path,
    *,
    intra_silence: float | None = None,
    inter_scene_silence: float | None = None,
    inter_chapter_silence: float | None = None,
    lead_in_silence: float | None = None,
    mix_title: str | None = None,
    progress: Callable[[str], None] = print,
) -> dict
```

Runs the whole stage and returns the manifest dict, which is also written to disk. The timeline follows the Stage-5 mix recorded in `book_dir/06_final/<mix_title>.timings.json`, loaded with [`s5_mix.load_mix_layout`](/library/stages/s5-mix/#load_mix_layout): its pads, lead-in, chapter list, measured chapter starts and embedded-picture order.

**Parameters**

- `book_id`: written to the manifest. `/` becomes `_` in the output EPUB name, so `level99/volume-01` gives `level99_volume-01.epub`.
- `book_dir`: `artifacts/<book>/`. The function reads `03_directed/*.json`, `05_audio/*/manifest.json`, `06_final/*.timings.json` and, for older sidecars, `00_book_meta.json` from here.
- `epub_path`: the **original** source EPUB. It is only read, never modified.
- `novel_dir`: `novels/<book>/`, which must contain `.epub_meta.json` from [`lnvox ingest-epub`](/stages/ingest-epub/). Its `images/` folder is used for picture alignment only when the sidecar records no picture list.
- `output_dir`: where the three outputs go. The CLI passes `artifacts/<book>/07_sync/`.
- `intra_silence`, `inter_scene_silence`, `inter_chapter_silence`, `lead_in_silence`: `None` (the default) takes the value the mix recorded, or `0.25` / `1.0` / `2.0` / `0.0` when there is no sidecar. A number overrides the recorded value, and a `[WARN]` is printed when it differs.
- `mix_title`: the `lnvox s5 --title` of the mix to follow. `None` means the newest sidecar in `06_final/`.
- `progress`: receives status lines, including `Following the mix in <sidecar>: pads …`, the per-chapter `✓ 01: matched 212/214 beats (3 tail-only)` line, the chapter-drift `[WARN]` and the span-coverage `[WARN]`. Default `print`.

With no sidecar at all, `run` prints `No 06_final/*.timings.json — …` and behaves as it did before sidecars: the pad arguments or their defaults, and every chapter in `05_audio/`.

**Returns** the manifest dict (schema [below](#sync_manifestjson)).

**Raises**

- `FileNotFoundError` if `novel_dir/.epub_meta.json` is missing, if `mix_title` names no sidecar (the message lists the mixes present), or if a chapter of the mix has no `05_audio/<id>/manifest.json`.
- `ValueError` if the mix was made from `05_audio_v2/` (VibeVoice sessions have no per-beat timing).
- `ValueError` if the EPUB's `container.xml` has no rootfile.
- `KeyError` if a chapter's OPF `href` is not present in the zip.

**Reads**

| Path | Used for |
|---|---|
| `novels/<book>/.epub_meta.json` | `chapters[].file` (the chapter id is the prefix before the first `-`), `chapters[].source_parts` (XHTML stems, `stem#anchor` for a `--split-chapters` ingest), `visual_elements` (lecture mode), `images` (embed order). |
| `artifacts/<book>/03_directed/*.json` | Beat `text`, `source_span`, `type`, `speaker`, `source_paragraph`. Beat ids are rebuilt as `f"{scene_id}_b{i:04d}"`, the same scheme Stage 4 uses. |
| `artifacts/<book>/06_final/<title>.timings.json` | The mix's layout ([sidecar format](/library/stages/s5-mix/#timingsjson-sidecar)). |
| `artifacts/<book>/05_audio/<chapter>/manifest.json` | `duration_seconds` and `scene_id` per beat of each mixed chapter, for timing. |
| `artifacts/<book>/00_book_meta.json`, `novels/<book>/images/` | Cover and picture order through [`s5_mix.resolve_book_images`](/library/stages/s5-mix/#resolve_book_images), only when the sidecar has no `embedded_images` or there is no sidecar. |
| The EPUB | `META-INF/container.xml`, the OPF manifest and spine, and every XHTML file. |

**Writes**: `<output_dir>/<book_id with / → _>.epub`, `<output_dir>/sync_manifest.json` and `<output_dir>/unmatched.json`.

**Flow**

1. Load the mix layout. Refuse a `05_audio_v2` mix, resolve each pad (argument, else recorded, else default) and warn about overrides.
2. Build `chapter_id → [stem, …]` from `.epub_meta.json`. For split pages, strip the `#anchor` (the OPF is keyed on the stem) but keep a `stem#anchor → chapter_id` map for [image ownership](#_image_owners).
3. Parse the OPF with `_parse_opf`: the OPF directory prefix, `stem → href` for every (X)HTML item, and the spine order.
4. Compute beat times with [`_compute_beat_timings`](#_compute_beat_timings), over the mixed chapters. Its chapter anchors are used only when every pad equals the recorded one, since they describe that layout.
5. For each chapter in `03_directed` order that is in the mix (a chapter left out by `lnvox s5 --chapters` is skipped and logged), build a **master shadow** by concatenating the shadows of all its `source_parts` pages. Stages 2 and 3 sometimes paraphrase bridge text at part boundaries, and matching per chapter instead of per part survives that. Run [Pass 1 and Pass 2](#the-matching-ladder), record manifest entries, place inline images, and wrap the matches.
6. Place [spine-level image pages](#image-placement) (light-novel inserts).
7. Place lecture-mode [visual elements](#visual_elements-in-lecture-mode).
8. Align `images` to the pictures the mix embedded: the sidecar's `embedded_images`, or `resolve_book_images` + `embedded_image_order` without overrides for an older sidecar.
9. Repack the EPUB, count the spans that actually landed, and write the manifest and `unmatched.json`.

### `align_images_to_embeds`

```python
def align_images_to_embeds(image_marks: list[dict], embedded: list[Path]) -> list[dict]
```

Produces exactly one entry per **embedded** m4b picture, in embed order (DESIGN.md §23.3). ln-reader pairs `images[i]` with the i-th picture in the `covr` atom by position.

- Image marks are grouped by the basename of their `src`. Each embedded picture then takes the first of its marks that has a `trigger_seconds`, or the first mark if none has one.
- A picture that no mark references still gets a placeholder entry: `src` is the picture's file name and every other field is `null`. Dropping it would shift every later picture by one slot, which was the original bug. In the demo run 0 of 148 Oz markers showed the right picture before the fix.
- The per-occurrence list is kept, unchanged, in `visual_elements`.

Each output entry has the keys `src`, `xhtml`, `chapter_id`, `trigger_seconds`, `after_beat_id` and `before_beat_id`.

### `place_visual_element`

```python
def place_visual_element(
    after_paragraph: int,
    seq: list[tuple[str, int, float, float]],
) -> tuple[str | None, str | None, float | None]
```

Places a lecture-mode block (code, table, figure, footnote or equation) in paragraph space. `seq` is the chapter's matched beats in playback order, as `(beat_id, source_paragraph, start_seconds, end_seconds)`.

The function returns `(after_beat_id, before_beat_id, trigger_seconds)`:

- `after_beat_id` is the last beat whose `source_paragraph <= after_paragraph`.
- `before_beat_id` is the first beat whose paragraph is greater.
- `trigger_seconds` is the start of the before-beat. For an element at the end of a chapter, which has no before-beat, it is the end of the after-beat.

See DESIGN.md §13.5.

## Shadow string and DOM map

### `_normalize_with_map`

```python
def _normalize_with_map(text: str) -> tuple[str, list[int]]
```

Returns the normalised text plus, for each normalised character, its index in the original string. The two lists always have the same length. Normalisation is applied on the search side only. The original casing and punctuation stay in the DOM.

| Transform | Effect |
|---|---|
| `‘ ’` → `'`, `“ ”` → `"` | 1 → 1 |
| `—` `–` → `-` | 1 → 1 |
| lowercase | 1 → 1, except codepoints that lowercase to several characters (`İ` → `i̇`). Those emit one map entry per output character, all pointing at the same original index, so later offsets do not shift. |
| any run of whitespace → one space | N → 1 |

Ligatures (`ﬁ`, `ﬂ`) and the ellipsis `…` are deliberately **not** expanded, because that would break the character mapping back to DOM offsets. The same normalisation is applied to `source_span` and beat `text` before matching.

### `_build_shadow`

```python
def _build_shadow(
    soup: BeautifulSoup,
) -> tuple[str, list[_NodeSegment], list[_ImageMark]]
```

Walks the `<body>` subtree in document order (falling back to the whole soup if there is no body) and builds three things:

- **Shadow string**: the concatenation of every visible text node, normalised. A single space is appended after each node that does not already end in one, so two adjacent nodes (`</p><p>`) cannot match across the boundary.
- **Segments**: one `_NodeSegment(shadow_start, shadow_end, node, norm_to_orig)` per text node. Together they are the DOM map from shadow positions back to `(text node, original offset)`.
- **Image marks**: one `_ImageMark(shadow_pos, src, node)` for each `<img>` or SVG `<image>`, recorded at the current shadow position. `src` is taken from `src`, else `xlink:href`, else `href`.

The walk skips several kinds of text:

- text inside `script`, `style`, `head` or `title`;
- whitespace-only nodes;
- every `PreformattedString`: comments, CDATA, processing instructions and doctypes. Before the 2026-07-03 fix, a match landing inside a comment turned hidden markup into visible text.
- anything outside `<body>`. BS4's `html.parser` exposes the `<?xml …?>` declaration as a document-level string, and wrapping it once produced spans before `<html>`, which strict XML parsers reject.

## The matching ladder

Matching is sequential and cursor-based per chapter, so repeated lines (`"Yes."` twice in a row) land on consecutive occurrences instead of the same one.

### Pass 1: strict, forward

For each beat in order:

1. **`span-exact`** ([`_find_source_span`](#_find_source_span)). The beat's normalised `source_span`, the verbatim source slice Stage 2 grounded it in, is searched for exactly in `[cursor, cursor + _MAX_FORWARD_JUMP)`. This is the common, unambiguous path.
2. If there is no span, or it did not match: [`_find_beat`](#_find_beat) with `strict=True` and the same forward cap. Only `exact` (short beats) and `anchored` (head and tail both found) are accepted.
3. On a hit the cursor moves to the match end. On a miss the beat is marked unmatched **and the cursor stays where it was**.

Lenient matches are not allowed in Pass 1. A loose match can land on a recurring phrase further on, jump the cursor past the real positions, and strand every later beat.

### Pass 2: lenient gap-fill (up to 3 rounds)

For each beat still unmatched:

- `prev_end` is the end of the nearest earlier match, or `0`. `next_start` is the start of the nearest later match, or the end of the shadow.
- The search range is `[max(0, prev_end - _PASS2_BACKWARD_SLACK), next_start)`. The backward slack is there because Stage 2 sometimes reorders attribution. The source has `Patrick admitted, '…'`, but Stage 2 emits the dialogue beat first and the `Patrick admitted` narration beat second, so the narration is found *before* the previous match. If the range is empty, the beat is skipped.
- First, `span-exact` inside the range. Then [`_find_beat_in_range`](#_find_beat_in_range): the lenient ladder on the gap slice, searched forward from the slice start, then fuzzy over the whole gap. Because the slice starts 2 000 characters before `prev_end`, a beat that belongs just before the previous match is found here.
- A hit's confidence gets a `+pass2` suffix, for example `tail-only+pass2`.
- Rounds repeat while they add matches, up to 3 rounds, because each new match narrows the gaps for its neighbours.

### Confidence values

| Value | Produced by | What is claimed |
|---|---|---|
| `span-exact` | `_find_source_span` | The whole normalised `source_span`. |
| `exact` | `_find_beat`, short beat (normalised length ≤ `HEAD_ANCHOR_LEN + TAIL_ANCHOR_LEN` = 60) | The whole beat text. |
| `anchored` | `_find_beat`: head (first 30 chars) found, then tail (last 30) found in `[head + 30, head + max(2·len + 300, 1000))` | Head start to tail end. |
| `head-only` | lenient: head found, tail not | The 30-char head anchor only. |
| `tail-only` | lenient: tail found forward, head not | The 30-char tail anchor only. |
| `fuzzy` | [`_fuzzy_match`](#_fuzzy_match) | The longest common substring only. |

The complete order, as the code runs it:

1. Pass 1, strict: `span-exact`, then `exact` (short beats) or `anchored` (long beats).
2. Pass 2, per gap: `span-exact`; then the lenient `_find_beat`, which for long beats tries `anchored`, `head-only`, `tail-only`, `fuzzy` and for short beats `exact`, `fuzzy`; then `fuzzy` over the whole gap.

Nothing searches backwards from a cursor. Lenient results only ever claim the verified anchor or substring, never the text from the cursor up to the anchor, so one over-reaching match cannot eat the next beat's text.

:::note[`backtrack` is retired]
An earlier lenient rung, `backtrack`, looked up to 1 500 characters before the cursor. It could never fire: Pass 1 is strict and never reaches it, and Pass 2 runs `_find_beat` on the gap slice with `cursor=0`. The Pass-2 backward slack (2 000 characters, searched forward) already covers that window, so the rung and its `_BACKTRACK_WINDOW` constant were removed. Removing them changed no match on the three local test books. The `"backtrack"` key stays in the histogram, always `0`, so existing manifest readers keep working.
:::

### `_find_source_span`

```python
def _find_source_span(
    shadow: str, span: str, cursor: int, max_forward: int
) -> tuple[int, int, str] | None
```

Normalises `span` and returns `(start, end, "span-exact")` if it occurs entirely within `shadow[cursor:cursor + max_forward]`. Returns `None` otherwise.

### `_find_beat`

```python
def _find_beat(
    shadow: str,
    beat_text: str,
    cursor: int,
    *,
    strict: bool = False,
    max_forward: int | None = None,
) -> tuple[int, int, str] | None
```

The head, tail and fuzzy ladder described above, applied to the lossy beat `text`, at or after `cursor`. `strict=True` returns only `exact` or `anchored` matches. `max_forward` bounds how far ahead the forward `exact`, head and anchored searches may look. `None` means unbounded, which is what Pass 2 uses inside an already bounded gap.

### `_fuzzy_match`

```python
def _fuzzy_match(
    shadow: str, norm_beat: str, cursor: int, max_window: int | None = None
) -> tuple[int, int, str] | None
```

Runs `difflib.SequenceMatcher(autojunk=False).find_longest_match` between the beat and `shadow[cursor:cursor + window]`:

- The window defaults to `max(_FUZZY_WINDOW_BASE, 4 × len(beat))`. The cap keeps runtime bounded: matching against a whole 30 kB chapter costs about 100 ms per call.
- The match is accepted only if the common substring is at least `max(20, int(len(beat) × _FUZZY_MIN_RATIO))` characters long.
- Only that substring is claimed, with no padding.

### `_find_beat_in_range`

```python
def _find_beat_in_range(
    shadow: str, beat_text: str, range_start: int, range_end: int
) -> tuple[int, int, str] | None
```

Pass 2's matcher. It slices `shadow[range_start:range_end]` and runs the lenient `_find_beat` on the slice. If that fails, it retries `_fuzzy_match` with `max_window=len(slice)`, so a long gap is not silently capped at the 3 000-char default. Returned offsets are translated back into master-shadow positions.

## Tuning constants

Current values, taken from `src/lnvox/stages/s6_sync.py`:

| Constant | Value | Effect |
|---|---|---|
| `HEAD_ANCHOR_LEN` / `TAIL_ANCHOR_LEN` | `30` / `30` | Anchor length. Beats with a normalised length of 60 or less use the whole-text `exact` path. Longer anchors mean fewer false positives but more misses on lightly paraphrased edges. |
| `_MAX_FORWARD_JUMP` | `8000` | Maximum distance ahead of the cursor for Pass 1 (`span-exact`, `exact`, head search). This is the single most important guard: without it, one bad anchored match leaping to the end of the chapter drops the whole tail. About 30 unmatched beats of about 250 chars still fit under the cap. |
| `_FUZZY_WINDOW_BASE` | `3000` | Minimum forward window for `_fuzzy_match`. |
| `_FUZZY_MIN_RATIO` | `0.20` | The fuzzy common substring must cover at least this fraction of the beat (and at least 20 chars). |
| `_PASS2_BACKWARD_SLACK` | `2000` | Backward extension of each Pass-2 gap. A local variable inside `run`, not a module constant. |
| `_SPAN_COVERAGE_FLOOR` | `0.75` | Warn if fewer than this share of matched beats appear as spans in the written EPUB. |
| `_MIX_DRIFT_TOLERANCE` | `0.5` | Seconds. Warn when a chapter's replayed length differs from the length s5 measured by more than this. |
| `_DEFAULT_PADS` | `0.25` / `1.0` / `2.0` / `0.0` | Intra, inter-scene, inter-chapter and lead-in when neither an argument nor a sidecar supplies them: the `lnvox s5` defaults. |

DESIGN.md §2.8 and the README list the same values for the matcher knobs.

## DOM wrapping

### `_wrap_matches`

```python
def _wrap_matches(
    soup: BeautifulSoup,
    segments: list[_NodeSegment],
    matches: list[_Match],
) -> None
```

Mutates `soup` in place:

1. For every match and every segment it overlaps, translate the shadow range into the node's original-text offsets through `norm_to_orig`.
2. Group the slices by text node (keyed by `id(node)`), sort them, and clamp overlaps.
3. Rebuild each node as `text / <span> / text / <span> / …` in one pass, so several beats per node work.
4. Replace the node by **identity**, not with `list.index()`. `NavigableString ==` compares string values, so with two identical text nodes under one parent the old code inserted the parts at the first twin and reordered the chapter.

A beat that crosses several text nodes (nested `<em>` or `<a>`, paragraph breaks) gets one span per node, all with the same `data-beat-id`. The player joins them.

```html
<p><span class="lnvox-beat" data-beat-id="01_s1_b0003">The gate was already open. </span><span class="lnvox-beat" data-beat-id="01_s1_b0004">“Who’s there?”</span> he called.</p>
```

`run` keeps **one soup per XHTML file, shared across chapters** (`soup_cache`), and only passes each soup the segments and matches that belong to it.

## Image placement

**Inline images** are `<img>` elements inside a chapter's own pages. An image at shadow position `P` gets these fields:

- `after_beat_id`: the last match whose span ends at or before `P`;
- `before_beat_id`: the first match that starts at or after `P`;
- `trigger_seconds`: the before-beat's start. For a **tailpiece** after the chapter's last matched beat, it is the after-beat's end. Left `null`, ln-reader would show it at 0:00 of the book (DESIGN.md §23.3).

On a page shared by several chapters, only the images owned by the current chapter are considered (see [`_image_owners`](#_image_owners)).

**Spine-level image pages**, such as light-novel `insert*`, `color*` and `bonus*` pages, are spine items that belong to no chapter:

- `after_beat_id` is the last beat of the nearest **preceding spine item** that has matches, and `before_beat_id` is the first beat of the nearest following one.
- `trigger_seconds` is the before-beat's start, or `null` for end matter with nothing after it.
- Placement is per XHTML part, not per chapter, so an insert between `chapter1.xhtml` and `chapter1_1.xhtml` triggers mid-chapter.
- These marks also carry `spine_page`, `after_chapter` and `before_chapter`.

### `_image_owners`

```python
def _image_owners(soup: BeautifulSoup, stem: str, key_to_chapter: dict[str, str]) -> dict[int, str]
```

Returns `id(<img>) → chapter_id` for a page that `--split-chapters` cut into several chapters, and `{}` for a single-chapter page. It uses the ingest split's own `chapter_markers` / `section_key` from [`lnvox.ingest.epub`](/library/ingest/epub/), so ownership follows the split exactly:

- text before the first marker belongs to the first section;
- a heading-only section, which produced no chapter, hands its pictures forward to the chapter that heading introduces;
- if there is no later chapter, the pictures go back to the previous one.

Before this, Oz's 148 pictures showed up as 1 453 occurrences (DESIGN.md §23.2).

## EPUB repack and verification

### `_write_modified_epub`

```python
def _write_modified_epub(
    orig_path: Path,
    modifications: dict[str, bytes],
    output_path: Path,
) -> None
```

Copies the source EPUB into a new one:

- `mimetype` is written **first and `ZIP_STORED`**, even when the source has it mid-archive. Some publisher EPUBs place it at entry 21–26, which epubcheck rejects.
- Every `.xhtml` / `.html` / `.htm` entry is written from `modifications` if present, else copied from the source. Either way it passes through [`_inject_responsive_css`](#_inject_responsive_css), with its compression type and attributes preserved.
- All other entries are copied byte for byte.

### `_inject_responsive_css`

```python
def _inject_responsive_css(xhtml_bytes: bytes) -> bytes | None
```

Appends `<style type="text/css" data-lnvox="lnvox-responsive">` to `<head>`:

```text
img,svg{max-width:100%!important;height:auto!important}table,pre{max-width:100%;overflow-x:auto}body{overflow-wrap:break-word}
```

- `!important` is needed to beat inline `style="width: 600px"` attributes.
- `height:auto` keeps the aspect ratio.
- Returns `None`, leaving the page unchanged, if the marker is already present (the injection is idempotent) or the page has no `<head>`.

This is applied to every XHTML page, including pages with no beats (DESIGN.md §21.2).

### `_count_beat_spans`

```python
def _count_beat_spans(epub_path: Path) -> int
```

Counts the unique `data-beat-id` values in the **written** EPUB. `run` compares this count against the number of matched beats and emits:

```text
Wrote 2239 beat span(s) into the EPUB (94% of matched beats).
  [WARN] only 102 of 1396 matched beat(s) reached the EPUB. Reader auto-follow will not highlight the rest — see DESIGN.md §21.1.
```

The first line is printed whenever at least one beat matched. The `[WARN]` line appears only below `_SPAN_COVERAGE_FLOOR` (75%). Healthy books sit at 88–97%: a match that overlaps no segment of a page never wraps.

## Timing

### `_compute_beat_timings`

```python
def _compute_beat_timings(
    audio_dir: Path,
    *,
    intra_silence: float,
    inter_scene_silence: float,
    inter_chapter_silence: float,
    lead_in_silence: float = 0.0,
    chapter_ids: list[str] | None = None,
    chapter_bounds: dict[str, tuple[float, float]] | None = None,
    warn: Callable[[str], None] | None = None,
) -> dict[str, tuple[float, float]]
```

Replays Stage 5's layout from the manifests and returns `beat_id → (start, end)` in m4b time, rounded to milliseconds.

- **Chapters.** `chapter_ids` is the list the mix contained, in mix order, from the sidecar. `None` means every sub-directory of `05_audio/` that has a `manifest.json`, sorted, which is what `lnvox s5` mixes without `--chapters`. A listed chapter with no manifest raises `FileNotFoundError`, because skipping it would shift every later chapter.
- **Pads.** Within a chapter the inter-scene pad is added when `scene_id` changes and the intra pad otherwise. The inter-chapter pad goes between chapters.
- **Lead-in.** `lead_in_silence` is added before every beat. A beat's start is where its lead-in starts, as in [scenario-sync](/library/stages/scenario-sync/#_beat_times), and its end is where its speech ends.
- **Anchors.** With `chapter_bounds` (the sidecar's probed `(start, end)` per chapter), each chapter starts at its recorded start, so millisecond rounding cannot accumulate and a chapter re-rendered after the mix cannot shift the ones after it. If a chapter's replayed length differs from its recorded length by more than `_MIX_DRIFT_TOLERANCE`, `warn` receives `[WARN] chapter <id>: its renders are ±N s off the mixed chapter …`.

A matched beat with no timing entry gets `(0.0, 0.0)` in the manifest. That only happens without a sidecar, for a directed chapter that was never rendered.

## `visual_elements` in lecture mode

`visual_elements` is the unified list the reader flips through (DESIGN.md §13.5). It contains:

1. every image mark, inline and spine-level, with `"kind": "image"` added, one entry per **occurrence**;
2. in lecture mode, every non-prose block recorded by ingest in `.epub_meta.json.visual_elements`: `code`, `table`, `figure`, `footnote` or `equation`. Each is placed with [`place_visual_element`](#place_visual_element) using its `after_paragraph` and the beats' `source_paragraph`, and carries:

```json
{
  "kind": "code",
  "src": "images/code_3f9a1c2b7d10.png",
  "html": "<pre><code class=\"language-python\">def mix(...)</code></pre>",
  "language": "python",
  "chapter_id": "04",
  "spine_page": "ch04",
  "after_beat_id": "04_s1_b0031",
  "before_beat_id": "04_s1_b0032",
  "trigger_seconds": 612.408
}
```

Code and tables were rendered to PNG at ingest, so they have a `src` and use the same image-flip path in the reader. Footnotes and equations usually carry only `html`. Narration-mode books have no ingest visual elements, and their beats have `source_paragraph = -1`.

## `sync_manifest.json`

Arrays are trimmed to a few entries.

```json title="artifacts/level99/volume-01/07_sync/sync_manifest.json"
{
  "book_id": "level99/volume-01",
  "epub": "artifacts/level99/volume-01/07_sync/level99_volume-01.epub",
  "m4b": "level99_volume-01.m4b",
  "span_class": "lnvox-beat",
  "data_attr": "data-beat-id",
  "total_beats": 2412,
  "matched": 2391,
  "unmatched": 21,
  "match_confidence": {
    "span-exact": 2270,
    "exact": 41,
    "anchored": 38,
    "head-only": 12,
    "tail-only": 13,
    "backtrack": 0,
    "fuzzy": 17
  },
  "silences": {
    "intra_seconds": 0.25,
    "inter_scene_seconds": 1.0,
    "inter_chapter_seconds": 2.0,
    "lead_in_seconds": 0.0
  },
  "images": [
    {
      "src": "../Images/cover.jpg",
      "xhtml": "OEBPS/Text/cover.xhtml",
      "chapter_id": null,
      "trigger_seconds": 0.0,
      "after_beat_id": null,
      "before_beat_id": "01_s1_b0000"
    },
    {
      "src": "../Images/insert1.jpg",
      "xhtml": "OEBPS/Text/insert1.xhtml",
      "chapter_id": null,
      "trigger_seconds": 842.117,
      "after_beat_id": "01_s9_b0014",
      "before_beat_id": "01_s10_b0000"
    },
    {
      "src": "back.jpg",
      "xhtml": null,
      "chapter_id": null,
      "trigger_seconds": null,
      "after_beat_id": null,
      "before_beat_id": null
    }
  ],
  "visual_elements": [
    {
      "src": "../Images/insert1.jpg",
      "xhtml": "OEBPS/Text/insert1.xhtml",
      "spine_page": "insert1",
      "after_chapter": "01",
      "before_chapter": "01",
      "after_beat_id": "01_s9_b0014",
      "before_beat_id": "01_s10_b0000",
      "trigger_seconds": 842.117,
      "kind": "image"
    }
  ],
  "beats": [
    {
      "beat_id": "01_s1_b0000",
      "data_beat_id": "01_s1_b0000",
      "chapter_id": "01",
      "xhtml": "OEBPS/Text/chapter1.xhtml",
      "type": "narration",
      "speaker": "Narrator",
      "start_seconds": 0.0,
      "end_seconds": 6.412,
      "match_confidence": "span-exact"
    },
    {
      "beat_id": "01_s1_b0001",
      "data_beat_id": "01_s1_b0001",
      "chapter_id": "01",
      "xhtml": "OEBPS/Text/chapter1.xhtml",
      "type": "dialogue",
      "speaker": "Kamijou Touma",
      "start_seconds": 6.662,
      "end_seconds": 8.905,
      "match_confidence": "anchored"
    },
    {
      "beat_id": "01_s2_b0000",
      "data_beat_id": "01_s2_b0000",
      "chapter_id": "01",
      "xhtml": "OEBPS/Text/chapter1.xhtml",
      "type": "narration",
      "speaker": "Narrator",
      "start_seconds": 9.905,
      "end_seconds": 14.371,
      "match_confidence": "tail-only+pass2"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `epub` | Path of the synced EPUB as a string. It is relative unless `LNVOX_ARTIFACTS_DIR` is absolute. |
| `m4b` | File name, in `06_final/`, of the m4b these timings describe. `null` when no sidecar was found. `lnvox sample` slices this file. |
| `total_beats` / `matched` / `unmatched` | Counts over every directed beat. |
| `match_confidence` | Histogram keyed by base confidence (the `+pass2` suffix is stripped). The `span-exact` share is a direct measure of Stage-2 grounding fidelity. `backtrack` is always `0` (a retired rung, kept for compatibility). |
| `silences` | The pad values the timings were computed with: `intra_seconds`, `inter_scene_seconds`, `inter_chapter_seconds`, `lead_in_seconds`. They equal the mix's sidecar unless a pad flag overrode it. |
| `images[]` | Exactly one entry per embedded m4b picture, in embed order (cover first). See [the `images[]` schema](#images-entries) below. |
| `visual_elements[]` | Every image occurrence (`kind: "image"`) plus lecture-mode blocks. |
| `beats[]` | Matched beats only, in playback order. `data_beat_id` equals `beat_id`, and `xhtml` is the zip path of the page where the match starts. `match_confidence` keeps the `+pass2` suffix. |

### `images[]` entries

`images[i]` describes the i-th picture stream of the m4b, and index 0 is the cover. ln-reader pairs them **by position**, because MP4's `covr` atom stores no per-picture name. The embed order comes from the mix's sidecar (`embedded_images`), so `lnvox s5 --cover` and `--images-dir` overrides are honoured. Every entry has the same six keys:

| Key | Type | Meaning |
|---|---|---|
| `src` | string | The `src` of the chosen `<img>` occurrence as written in the XHTML (for example `../Images/insert1.jpg`). For a picture no page references, the embedded file's name. |
| `xhtml` | string or `null` | Zip path of the page holding that occurrence. `null` for an unreferenced picture. |
| `chapter_id` | string or `null` | The chapter whose text holds the picture inline. `null` for spine-level image pages (inserts, cover pages) and unreferenced pictures. |
| `trigger_seconds` | number or `null` | When to show it, in m4b time: the start of `before_beat_id`, or for a tailpiece the end of `after_beat_id`. `null` when no matched beat follows it: end matter after the last chapter, and pictures no page references (often the cover image itself). A front-matter page's picture triggers at the first beat. |
| `after_beat_id` | string or `null` | The last matched beat before the picture. |
| `before_beat_id` | string or `null` | The first matched beat after the picture. |

Pictures are matched to occurrences by file name. Each picture takes its first occurrence that has a `trigger_seconds`, or its first occurrence if none has one. A picture that is embedded but referenced nowhere keeps its slot with `null`s, since dropping it would shift every later picture by one. The other occurrences of a picture, and spine-page details such as `spine_page`, `after_chapter` and `before_chapter`, stay in `visual_elements[]`.

## `unmatched.json`

```json title="artifacts/level99/volume-01/07_sync/unmatched.json"
{
  "beats": [
    {
      "beat_id": "07_s3_b0012",
      "chapter_id": "07",
      "text_preview": "She never said it out loud, but everyone in the guild knew the debt was his.",
      "reason": "no anchor in chapter source (pass1+pass2)"
    },
    {
      "beat_id": "12_s1_b0000",
      "chapter_id": "12",
      "text_preview": "Afterword",
      "reason": "no source_parts for chapter"
    }
  ]
}
```

`text_preview` is the first 120 characters of the beat's `text`. The usual cause is a genuine Stage-2 paraphrase or hallucination. A whole chapter reported with `no source_parts for chapter` means `.epub_meta.json` has no stems for that id.

## Notes

- **§21 fixes, with the tests that guard them.**
  - Shared soup per XHTML (§21.1, `tests/test_s6_split_chapters.py`). Before it, Oz kept 102 of 1 396 spans and Flatland 23 of 687.
  - Responsive CSS on every page (§21.2, `tests/test_s6_epub_output.py`).
  - The written-span count and `[WARN]` (§21.3). "Beats matched" stayed green through both shipped defects.
  - Also guarded: `mimetype` first and stored; node identity in wrapping, comment exclusion and multi-character lowercase mapping (`tests/test_s6_sync.py`); one `images` entry per embedded picture and per-section image ownership (`tests/test_m4b_images.py`).
- **Layout from the sidecar** (`tests/test_mix_layout.py`). A real ffmpeg mix with a lead-in, a `--chapters` subset and picture overrides is replayed without any flag, and the last beat must end where the m4b ends. Old sidecars, no sidecar, overrides, a `--v2` mix and a chapter re-rendered after the mix are covered too. The Pass-2 slack placing a beat that Stage 2 moved after its neighbour is in `tests/test_s6_sync.py`.
- **Measured.** Before `source_span` existed, level99 v1 matched 98.9% and v2 95.7% using the fuzzy ladder alone. The unmatched remainder were Stage-2 hallucinations.
- **Why `source_paragraph` is not used for narration.** Stage 2's `start_paragraph` / `end_paragraph` index the chapter `.txt`, which is a different coordinate space from the XHTML shadow. Exact `source_span` matching plus the forward cap does the job that a scene window would (DESIGN.md §2.8).
- **Deterministic and uncached.** The same inputs give the same outputs, and each run rewrites all three files. The stage is CPU-only and runs in seconds to minutes per book.
