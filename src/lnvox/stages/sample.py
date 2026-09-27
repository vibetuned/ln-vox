"""Build a small, committable demo sample from a finished book (DESIGN.md §22).

A rendered book is 150-220 MB. This cuts a window that still exercises every
ln-reader feature — auto-follow, chapter-relative scrubbing, the chapter
transition, and inline images — by straddling a chapter boundary rather than
taking whole chapters.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChapterSpan:
    chapter_id: str
    title: str
    start: float
    end: float


def chapter_spans(manifest: dict, titles: dict[str, str]) -> list[ChapterSpan]:
    """Per-chapter [start, end] in book time, from the manifest's beats."""
    bounds: dict[str, list[float]] = defaultdict(lambda: [float("inf"), 0.0])
    for beat in manifest["beats"]:
        b = bounds[beat["chapter_id"]]
        b[0] = min(b[0], float(beat["start_seconds"]))
        b[1] = max(b[1], float(beat["end_seconds"]))
    return [
        ChapterSpan(cid, titles.get(cid, cid), lo, hi)
        for cid, (lo, hi) in sorted(bounds.items())
    ]


# A sample is meant to demonstrate image display, so a pair is only eligible
# if its realised window actually contains this many. Below the threshold the
# pick falls back to whichever window has the most.
_MIN_IMAGES = 3


def score_pairs(
    manifest: dict,
    spans: list["ChapterSpan"],
    minutes: float,
) -> list[tuple[int, int, str, str]]:
    """Rank consecutive chapter pairs by what the SAMPLE would contain.

    Scoring whole chapters is the wrong unit: the window is 12 minutes of a
    30-60 minute pair, and illustrations cluster, so a chapter pair rich in
    images can still yield a window with one. Every candidate is therefore
    evaluated on its realised window.

    Returns `(images, speakers, first, second)`, best first: the pairs in the
    highest image tier that anyone reaches (>= _MIN_IMAGES, else >= 1, else
    all), ranked by speaker count. Multi-voice dialogue is the headline
    feature, so it decides among pairs that already show the image viewer
    rather than competing with it.
    """
    image_times = [
        float(v["trigger_seconds"])
        for v in (manifest.get("visual_elements") or [])
        if v.get("trigger_seconds") is not None
    ]
    speakers: dict[str, set[str]] = defaultdict(set)
    for beat in manifest["beats"]:
        if beat.get("speaker"):
            speakers[beat["chapter_id"]].add(beat["speaker"])

    ids = [s.chapter_id for s in spans]
    rated: list[tuple[int, int, str, str]] = []
    for a, b in zip(ids, ids[1:]):
        lo, hi = pick_window(spans, a, b, minutes, image_times)
        n_img = sum(1 for t in image_times if lo <= t <= hi)
        n_spk = len(speakers[a] | speakers[b])
        rated.append((n_img, n_spk, a, b))

    # Tiers, not a single sort: once a window shows the image viewer at all,
    # more voices beat more pictures. Maximising images outright when nothing
    # reached _MIN_IMAGES picked a Flatland window with 2 speakers over one
    # with 6, for the sake of a second diagram.
    for floor in (_MIN_IMAGES, 1, 0):
        tier = [r for r in rated if r[0] >= floor]
        if tier:
            tier.sort(key=lambda r: (r[1], r[0]), reverse=True)
            return tier
    return rated


# The boundary must sit at least this far from either edge of the window, as a
# fraction of its length. A transition 20 seconds from the end is technically
# present but useless to demo.
_BOUNDARY_MARGIN = 0.25


def pick_window(
    spans: list[ChapterSpan],
    first: str,
    second: str,
    minutes: float,
    image_times: list[float] | None = None,
) -> tuple[float, float]:
    """Choose a window of `minutes` that spans the boundary between two chapters.

    The transition is one of the features being demonstrated, so it must sit
    comfortably inside the slice — not at its edge. Within that constraint the
    window slides to cover as many illustrations as possible: centring rigidly
    on the boundary captured just 1 image of Alice's 4 and 1 of Fairy Dreams',
    because images cluster rather than spreading evenly.
    """
    by_id = {s.chapter_id: s for s in spans}
    a, b = by_id[first], by_id[second]
    length = minutes * 60.0
    boundary = b.start

    lo_bound = max(a.start, boundary - length * (1.0 - _BOUNDARY_MARGIN))
    hi_bound = min(b.end - length, boundary - length * _BOUNDARY_MARGIN)
    if hi_bound < lo_bound:                     # chapters too short to slide
        start = max(a.start, boundary - length / 2.0)
        return start, min(b.end, start + length)

    if not image_times:
        start = max(lo_bound, min(hi_bound, boundary - length / 2.0))
        return start, start + length

    # Sample candidate offsets and keep the one covering the most images;
    # ties break toward the most centred boundary.
    steps = 40
    best = None
    for i in range(steps + 1):
        cand = lo_bound + (hi_bound - lo_bound) * i / steps
        n = sum(1 for t in image_times if cand <= t <= cand + length)
        centred = -abs((boundary - cand) - length / 2.0)
        key = (n, centred)
        if best is None or key > best[0]:
            best = (key, cand)
    start = best[1]
    return start, start + length


def _ffmetadata(spans: list[ChapterSpan], window: tuple[float, float], title: str) -> str:
    """ffmetadata chapter markers, clipped to the window and rebased to zero."""
    start, end = window
    lines = [";FFMETADATA1", f"title={title}"]
    for s in spans:
        lo, hi = max(s.start, start), min(s.end, end)
        if hi - lo <= 0.5:          # a sliver of a chapter is not a chapter
            continue
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round((lo - start) * 1000))}",
            f"END={int(round((hi - start) * 1000))}",
            f"title={s.title}",
        ]
    return "\n".join(lines) + "\n"


def select_pictures(
    images: list[dict], window: tuple[float, float]
) -> list[int]:
    """Embedded-picture indices a sample carries: the cover, then every
    picture whose playback time falls in the window, in embed order.

    Index 0 is always kept because ln-reader treats the first embedded picture
    as the cover, and `images` must stay aligned to what gets embedded.
    """
    start, end = window
    keep = [0] if images else []
    for k, im in enumerate(images[1:], start=1):
        t = im.get("trigger_seconds")
        if t is not None and start <= float(t) <= end:
            keep.append(k)
    return keep


def _slice_audio(
    src: Path,
    dst: Path,
    window: tuple[float, float],
    meta: Path,
    pictures: list[int],
) -> None:
    """Cut the window, then re-attach the cover and in-window pictures.

    Two passes, both `-c copy` — no re-encode, so the sample is bit-identical
    to the shipped render. It has to be two: an attached picture is a single
    packet at pts 0, so ANY seek past 0 drops it. That is exactly how the first
    samples shipped with no cover and no images. Pass 1 cuts the audio with an
    exact output seek; pass 2 muxes it with pictures read from an unseeked copy
    of the source.
    """
    start, end = window
    audio_only = dst.with_suffix(".audio.m4a")
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src), "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-map", "0:a", "-c", "copy", str(audio_only),
    ], check=True)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(audio_only), "-i", str(src), "-i", str(meta),
        "-map", "0:a",
    ]
    for k in pictures:
        cmd += ["-map", f"1:v:{k}"]
    cmd += ["-map_metadata", "2", "-map_chapters", "2", "-c", "copy"]
    for out_idx in range(len(pictures)):
        cmd += [f"-disposition:v:{out_idx}", "attached_pic"]
    cmd.append(str(dst))
    try:
        subprocess.run(cmd, check=True)
    finally:
        audio_only.unlink(missing_ok=True)


def _spans_in_epub(epub_path: Path) -> set[str]:
    """Every `data-beat-id` actually present in the EPUB."""
    import re
    import zipfile

    found: set[str] = set()
    with zipfile.ZipFile(epub_path, "r") as z:
        for name in z.namelist():
            if name.lower().endswith((".xhtml", ".html", ".htm")):
                found.update(
                    m.decode() for m in re.findall(rb'data-beat-id="([^"]+)"', z.read(name))
                )
    return found


def _rebase(
    manifest: dict,
    window: tuple[float, float],
    present_spans: set[str] | None = None,
    pictures: list[int] | None = None,
) -> dict:
    """Keep only what falls inside the window, with timings rebased to zero.

    `present_spans` drops beats whose `data-beat-id` never made it into the
    EPUB. Stage 6 matches more beats than it manages to wrap (88-97% on the
    demo books), and a manifest entry with no span is a timing pointing at
    nothing — harmless in a full book, but in a sample it is most of what a
    tester would poke at.
    """
    start, end = window
    beats = [
        {**b,
         "start_seconds": round(float(b["start_seconds"]) - start, 3),
         "end_seconds": round(float(b["end_seconds"]) - start, 3)}
        for b in manifest["beats"]
        if float(b["start_seconds"]) >= start and float(b["end_seconds"]) <= end
        and (present_spans is None or b.get("data_beat_id") in present_spans)
    ]
    kept_ids = {b["beat_id"] for b in beats}
    visuals = []
    for ve in manifest.get("visual_elements") or []:
        t = ve.get("trigger_seconds")
        if t is None or not (start <= float(t) <= end):
            continue
        v = {**ve, "trigger_seconds": round(float(t) - start, 3)}
        # Anchors pointing outside the window would dangle in the reader.
        for k in ("after_beat_id", "before_beat_id"):
            if v.get(k) and v[k] not in kept_ids:
                v[k] = None
        visuals.append(v)
    images = []
    for k in pictures or []:
        im = dict(manifest["images"][k])
        t = im.get("trigger_seconds")
        # The cover (index 0) usually has no time; one outside the window is
        # meaningless here. Either way None, which the reader places at 0.
        im["trigger_seconds"] = (
            round(float(t) - start, 3)
            if t is not None and start <= float(t) <= end else None
        )
        for key in ("after_beat_id", "before_beat_id"):
            if im.get(key) and im[key] not in kept_ids:
                im[key] = None
        images.append(im)

    return {
        **{k: v for k, v in manifest.items()
           if k not in ("beats", "visual_elements", "images")},
        "images": images,
        "sample": {
            "window_start_seconds": round(start, 3),
            "window_end_seconds": round(end, 3),
            "of_book": manifest.get("book_id", ""),
        },
        "total_beats": len(beats),
        "matched": len(beats),
        "beats": beats,
        "visual_elements": visuals,
    }


_README = """# {title} — ln-reader demo sample

A {minutes:.0f}-minute slice of a full ln-vox render, small enough to commit.
It straddles a chapter boundary so every reader feature is exercisable:

| Feature | How to check it |
|---|---|
| Text-highlight auto-follow | Open the EPUB alongside the audio; the current beat highlights as it plays. |
| Chapter-relative scrubbing | Two chapters are present — scrub within one, then across the boundary. |
| Chapter transition | The boundary sits near the middle of the sample, at ~{boundary:.1f} min. |
| Image viewer | The cover plus {n_images} illustration(s) are embedded; each shows as a scrubber marker at its playback time. |

## Files

| File | What it is |
|---|---|
| `{audio_name}` | The audio slice plus the cover and in-window pictures — `-c copy` from the real render, no re-encode. |
| `{epub_name}` | The **complete** synced EPUB (it is the small half). |
| `sync_manifest.json` | {n_beats} beats and one `images` entry per embedded picture (cover first), timings rebased so the sample starts at 0. |

## Chapters in this sample

{chapter_list}

Cut from `{book_id}` covering {start:.1f}–{end:.1f} min of the full book.
Beats outside that range have no timing here — that is what makes it a sample.
"""


def build(
    book_dir: Path,
    novel_dir: Path,
    out_dir: Path,
    *,
    book_title: str,
    chapters: tuple[str, str] | None = None,
    minutes: float = 12.0,
    progress=print,
) -> dict:
    """Cut a demo sample. Returns a summary dict."""
    sync_dir = book_dir / "07_sync"
    manifest_path = sync_dir / "sync_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No {manifest_path}. Run s6 first — a sample needs the sync layer."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    src_epub = next(iter(sync_dir.glob("*.epub")), None)
    src_audio = next(iter((book_dir / "06_final").glob("*.m4b")), None)
    if src_epub is None or src_audio is None:
        raise FileNotFoundError(
            f"Need both {book_dir}/06_final/*.m4b and {sync_dir}/*.epub."
        )

    meta = json.loads((novel_dir / ".epub_meta.json").read_text(encoding="utf-8"))
    titles = {c["file"].split("-", 1)[0]: c["title"] for c in meta.get("chapters", [])}
    spans = chapter_spans(manifest, titles)

    if chapters is None:
        ranked = score_pairs(manifest, spans, minutes)
        if not ranked:
            raise ValueError("Book has fewer than two chapters with beats.")
        n_img, n_spk, first, second = ranked[0]
        note = "" if n_img >= _MIN_IMAGES else " — best available, few images"
        progress(
            f"Auto-picked chapters {first}+{second}: "
            f"{n_spk} speakers, {n_img} image(s) in the window{note}"
        )
    else:
        first, second = chapters

    image_times = [
        float(v["trigger_seconds"])
        for v in (manifest.get("visual_elements") or [])
        if v.get("trigger_seconds") is not None
    ]
    window = pick_window(spans, first, second, minutes, image_times)
    start, end = window
    progress(
        f"Window {start / 60:.1f}-{end / 60:.1f} min of the book "
        f"({(end - start) / 60:.1f} min)"
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    slug = book_dir.name
    audio_name, epub_name = f"{slug}-sample.m4b", f"{slug}-sample.epub"

    ffmeta = out_dir / "ffmetadata.txt"
    ffmeta.write_text(_ffmetadata(spans, window, book_title), encoding="utf-8")
    pictures = select_pictures(manifest.get("images") or [], window)
    _slice_audio(src_audio, out_dir / audio_name, window, ffmeta, pictures)
    ffmeta.unlink()
    shutil.copy2(src_epub, out_dir / epub_name)

    sample_manifest = _rebase(
        manifest, window, _spans_in_epub(src_epub), pictures
    )
    sample_manifest["epub"] = epub_name
    (out_dir / "sync_manifest.json").write_text(
        json.dumps(sample_manifest, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    in_window = [s for s in spans if min(s.end, end) - max(s.start, start) > 0.5]
    chapter_list = "\n".join(
        f"{i}. **{s.title}** — starts at {max(0.0, s.start - start) / 60:.1f} min"
        for i, s in enumerate(in_window, start=1)
    )
    boundary = (spans[[s.chapter_id for s in spans].index(second)].start - start) / 60
    (out_dir / "README.md").write_text(_README.format(
        title=book_title, minutes=(end - start) / 60, boundary=boundary,
        n_images=max(0, len(sample_manifest["images"]) - 1),
        n_beats=len(sample_manifest["beats"]),
        audio_name=audio_name, epub_name=epub_name,
        book_id=manifest.get("book_id", slug),
        start=start / 60, end=end / 60, chapter_list=chapter_list,
    ), encoding="utf-8")

    return {
        "chapters": [s.chapter_id for s in in_window],
        "beats": len(sample_manifest["beats"]),
        "visual_elements": len(sample_manifest["visual_elements"]),
        "pictures": len(sample_manifest["images"]),
        "seconds": round(end - start, 1),
        "audio": str(out_dir / audio_name),
    }
