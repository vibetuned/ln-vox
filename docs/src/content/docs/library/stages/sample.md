---
title: lnvox.stages.sample
description: Cut a small, committable ln-reader demo from a finished book — chapter-pair scoring, boundary-straddling window, -c copy slice, rebased manifest.
sidebar:
  order: 11
  label: sample
---

`lnvox.stages.sample` cuts a demo sample of about 12 minutes from a finished book. The audio of a full book is 150–220 MB. The sample is small enough to commit to a public repo and still exercises every ln-reader feature: text-highlight auto-follow, chapter-relative scrubbing, the chapter transition, and inline images. It is called by `lnvox sample` ([utilities page](/stages/utilities/#lnvox-sample)) and needs the outputs of both [Stage 5](/library/stages/s5-mix/) and [Stage 6](/library/stages/s6-sync/) (DESIGN.md §22, §23.4–23.5).

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`build`](#build) | function | Entry point: pick chapters and window, slice, rebase, write README. |
| [`ChapterSpan`](#chapterspan), [`chapter_spans`](#chapter_spans) | dataclass / function | Per-chapter `[start, end]` in book time. |
| [`score_pairs`](#score_pairs) | function | Rank consecutive chapter pairs by what their **window** would contain. |
| [`pick_window`](#pick_window) | function | A window of `minutes` straddling the pair's boundary, slid to cover images. |
| [`select_pictures`](#select_pictures) | function | Embedded-picture indices to keep: the cover plus the pictures inside the window. |
| [`pick_source_audio`](#pick_source_audio) | function | Which `06_final/*.m4b` to slice, so the audio matches the manifest's timings. |
| `_slice_audio`, `_ffmetadata`, `_rebase`, `_spans_in_epub` | private | Two-pass `-c copy` cut, clipped chapter markers, manifest rebasing, span inventory. |
| `_MIN_IMAGES = 3`, `_BOUNDARY_MARGIN = 0.25` | constants | Image tier threshold, and the minimum distance of the boundary from either window edge. |

## API

### `build`

```python
def build(
    book_dir: Path,
    novel_dir: Path,
    out_dir: Path,
    *,
    book_title: str,
    chapters: tuple[str, str] | None = None,
    minutes: float = 12.0,
    mix_title: str | None = None,
    progress=print,
) -> dict
```

**Parameters**

- `book_dir`: `artifacts/<book>/`. Must contain `07_sync/sync_manifest.json`, the synced EPUB and at least one `06_final/*.m4b`.
- `novel_dir`: `novels/<book>/`. Its `.epub_meta.json` provides the chapter titles.
- `out_dir`: created if missing.
- `book_title`: the `title` metadata of the sample m4b and the README heading.
- `chapters`: two chapter ids `(first, second)`. The window straddles the start of `second`. With `None`, the top pair from [`score_pairs`](#score_pairs) is used. The ids are not checked for being consecutive.
- `minutes`: window length. Default `12.0`.
- `mix_title`: an `lnvox s5 --title`. When `06_final/<mix_title>.m4b` exists, that file is sliced. The CLI passes its `--title`, which is also the sample's title, so a title that names no mix simply falls through to the next choice. See [`pick_source_audio`](#pick_source_audio).
- `progress`: status sink. Default `print`. It receives `Slicing <file>.m4b` and any mismatch `[WARN]`.

**Returns** a summary dict:

```json
{
  "chapters": ["11", "12"],
  "beats": 214,
  "visual_elements": 5,
  "pictures": 4,
  "seconds": 720.0,
  "audio": "samples/oz_volume-01/volume-01-sample.m4b"
}
```

`chapters` lists the chapters that overlap the window by more than 0.5 s. `pictures` counts embedded pictures, cover included.

**Raises**

- `FileNotFoundError` when there is no sync manifest ("Run s6 first"), no m4b or synced EPUB, or no `.epub_meta.json`.
- `ValueError` when fewer than two chapters have beats.
- `KeyError` for an unknown chapter id.
- `subprocess.CalledProcessError` if ffmpeg fails.

**Writes** into `out_dir`. `<slug>` is `book_dir.name`, for example `volume-01`.

| File | Content |
|---|---|
| `<slug>-sample.m4b` | The audio window with the cover and in-window pictures and clipped chapter markers. It is a `-c copy` of the real render, not a re-encode. |
| `<slug>-sample.epub` | A copy of the **complete** synced EPUB (`shutil.copy2`). |
| `sync_manifest.json` | The rebased manifest (see [`_rebase`](#what-ships-in-a-sample)), written with `indent=1`. Its `epub` and `m4b` keys name the sample's own files. |
| `README.md` | A feature checklist, a file table, and the chapter list with start times inside the sample. |

`ffmetadata.txt` and the temporary `<slug>-sample.audio.m4a` are deleted after use.

The source files are chosen, not globbed. The EPUB is the one the manifest's `epub` key names (tried as given, then by file name inside `07_sync/`), else the first `07_sync/*.epub` in sorted order. The m4b comes from [`pick_source_audio`](#pick_source_audio).

### `pick_source_audio`

```python
def pick_source_audio(
    final_dir: Path, manifest: dict, mix_title: str | None = None, progress=print
) -> Path | None
```

The m4b to slice. `06_final/` can hold several m4b files, because a re-mix under a new `--title` leaves the old one behind, and the first glob hit used to be sliced, often the stale one. The first candidate that exists wins:

1. `06_final/<safe_filename(mix_title)>.m4b`, when `mix_title` is given;
2. the m4b Stage 6 timed the manifest against (the manifest's `m4b` key);
3. the m4b of the newest `.timings.json` sidecar, which is written only once its m4b is complete;
4. the newest `*.m4b` by modification time.

If the pick is not the manifest's own `m4b`, `progress` receives `[WARN] slicing X.m4b, but sync_manifest.json was timed against Y.m4b — the sample may be out of sync.` Returns `None` when there is no m4b at all.

### `ChapterSpan`

```python
@dataclass(frozen=True)
class ChapterSpan:
    chapter_id: str
    title: str
    start: float
    end: float
```

### `chapter_spans`

```python
def chapter_spans(manifest: dict, titles: dict[str, str]) -> list[ChapterSpan]
```

For each chapter: the minimum `start_seconds` and maximum `end_seconds` of its **matched** beats in `manifest["beats"]`, sorted by chapter id. `titles` maps chapter id to title. Ids missing from it fall back to the id itself.

### `score_pairs`

```python
def score_pairs(
    manifest: dict,
    spans: list["ChapterSpan"],
    minutes: float,
) -> list[tuple[int, int, str, str]]
```

Ranks every consecutive pair `(a, b)` by what the **sample itself** would contain, not by the whole chapters. Illustrations cluster, so a pair rich in pictures can still produce a 12-minute window with only one.

1. Image times are every `visual_elements[].trigger_seconds` that is not `null`.
2. For each pair, [`pick_window`](#pick_window) is run with those times. `images` counts the times inside the window. `speakers` counts the distinct non-empty `speaker` values over **both whole chapters**.
3. **Tiers.** Keep the pairs with `images >= _MIN_IMAGES` (3). If there are none, keep those with `images >= 1`. If there are still none, keep all. Within the tier, sort by `(speakers, images)`, descending.

Multi-voice dialogue is the headline feature, so it decides among pairs that already show the image viewer instead of competing with pictures. Before the tiers, Flatland (12 pictures, no window with three) chose a two-voice window over the six-voice arrival of the Sphere.

Returns `(images, speakers, first, second)` tuples, best first. `build` logs `— best available, few images` when the winner has fewer than 3 images.

### `pick_window`

```python
def pick_window(
    spans: list[ChapterSpan],
    first: str,
    second: str,
    minutes: float,
    image_times: list[float] | None = None,
) -> tuple[float, float]
```

Chooses `[start, start + L]` with `L = minutes × 60` around `boundary = second.start`.

- The boundary must sit at least `_BOUNDARY_MARGIN × L` (25%) from either edge, which keeps `start` within `[boundary − 0.75·L, boundary − 0.25·L]`. `start` is also kept within the two chapters: `start ≥ first.start` and `start + L ≤ second.end`.
- **Chapters too short to slide** (the bounds cross): `start = max(first.start, boundary − L/2)` and `end = min(second.end, start + L)`. The window can then be shorter than `minutes`.
- **No image times**: the window is centred on the boundary, clamped to the bounds.
- **With image times**: 41 evenly spaced candidate starts between the bounds are tried. The one covering the most images wins, and ties go to the most centred boundary. Centring rigidly caught 1 of Alice's 4 images.

### `select_pictures`

```python
def select_pictures(
    images: list[dict], window: tuple[float, float]
) -> list[int]
```

Indices into the manifest `images` array, which is also the order of the m4b's embedded pictures. Index `0`, the cover, is always kept, because ln-reader treats the first embedded picture as the cover. After it come the pictures whose `trigger_seconds` falls inside the window, in embed order.

## Chapter-pair scoring, end to end

```text
sync_manifest.json ─► chapter_spans ─► score_pairs ──► (first, second)
                             │              └─ pick_window per pair
                             └──────────────► pick_window(first, second, image_times) ─► window
```

With `--chapters 14,15`, scoring is skipped, but the window is still slid to cover images.

## What ships in a sample

**Audio (`_slice_audio`).** The cut takes two `-c copy` passes and never re-encodes. An attached picture is a single packet at pts 0, so any seek past 0 drops it. That is how the first samples shipped with no cover (DESIGN.md §23.4).

1. `ffmpeg -i book.m4b -ss <start> -to <end> -map 0:a -c copy <slug>-sample.audio.m4a`, an exact output seek on the audio only.
2. `ffmpeg -i audio.m4a -i book.m4b -i ffmetadata.txt -map 0:a -map 1:v:<k>… -map_metadata 2 -map_chapters 2 -c copy -disposition:v:N attached_pic`. The pictures are read from the **unseeked** source.

**Chapter markers (`_ffmetadata`).** Each chapter is clipped to the window and rebased so the window starts at 0. A chapter with 0.5 s or less inside the window is dropped: a sliver is not a chapter.

**Manifest (`_rebase`).**

- `beats`: only beats lying entirely inside the window **and** whose `data_beat_id` actually appears in the EPUB (`_spans_in_epub`). Stage 6 matches more beats than it manages to wrap, and a timing that points at no span is most of what a tester would click on. Times are rebased by subtracting the window start.
- `visual_elements`: only those whose `trigger_seconds` is inside the window, rebased. `after_beat_id` or `before_beat_id` values that point at beats that were not kept are set to `null`.
- `images`: exactly the selected pictures, in the same order as the sample m4b's picture streams. `trigger_seconds` is rebased when inside the window and `null` otherwise (the cover usually). Dangling anchors are set to `null`.
- `sample`: `{window_start_seconds, window_end_seconds, of_book}`. `total_beats` and `matched` become the kept beat count. `epub` and `m4b` become the sample's own file names.
- Every other top-level key is copied unchanged from the full manifest (`book_id`, `span_class`, `data_attr`, `match_confidence`, `silences`, `unmatched`), so those numbers still describe the whole book. In the excerpt below, `unmatched: 57` is the full book's count; `match_confidence` and `silences` are left out.

```json title="samples/oz_volume-01/sync_manifest.json (excerpt)"
{
 "book_id": "oz/volume-01",
 "epub": "volume-01-sample.epub",
 "m4b": "volume-01-sample.m4b",
 "span_class": "lnvox-beat",
 "data_attr": "data-beat-id",
 "total_beats": 214,
 "matched": 214,
 "unmatched": 57,
 "images": [
  {"src": "../images/cover.jpg", "xhtml": "OEBPS/cover.xhtml", "chapter_id": null,
   "trigger_seconds": null, "after_beat_id": null, "before_beat_id": null},
  {"src": "../images/emerald-gate.jpg", "xhtml": "OEBPS/text02.xhtml", "chapter_id": "11",
   "trigger_seconds": 131.207, "after_beat_id": "11_s2_b0009", "before_beat_id": "11_s2_b0010"}
 ],
 "sample": {"window_start_seconds": 24110.502, "window_end_seconds": 24830.502, "of_book": "oz/volume-01"},
 "beats": [
  {"beat_id": "11_s2_b0010", "data_beat_id": "11_s2_b0010", "chapter_id": "11",
   "xhtml": "OEBPS/text02.xhtml", "type": "narration", "speaker": "Narrator",
   "start_seconds": 131.207, "end_seconds": 137.95, "match_confidence": "span-exact"}
 ],
 "visual_elements": []
}
```

**EPUB.** It ships whole: 0.4–11 MB against 150–220 MB of audio. Trimming it would mean rewriting the OPF spine and nav for no real saving. The reader shows the complete book, and beats outside the window simply have no timing.

## Notes

- **Picture alignment depends on Stage 6.** `select_pictures` indexes the manifest `images` and `_slice_audio` maps `1:v:<k>` from the source m4b. That only works because Stage 6 writes exactly one `images` entry per embedded picture, in embed order ([`align_images_to_embeds`](/library/stages/s6-sync/#align_images_to_embeds)).
- **Tests.** `tests/test_sample.py` covers the window margin and sliding, the tiers, rebasing and span filtering, and ffmetadata clipping. `tests/test_m4b_images.py` checks with ffmpeg that the sliced sample still carries byte-identical `covr` pictures. `tests/test_mix_layout.py` covers the m4b choice with a stale mix present.
- **DESIGN.md §22.3 is stale.** It describes a `3 × speakers + min(images, 40)` score over whole chapters. The code uses the tiered, window-based ranking described in §23.5 and above.
