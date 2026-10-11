"""Stage 5: Concatenate all rendered beats into a single .m4b audiobook.

Pipeline:
    1. Generate three silence WAVs (intra-scene, inter-scene, inter-chapter).
    2. For each chapter, build an ffmpeg concat list interleaving beat WAVs
       with the appropriate silence and concat to a single chapter WAV.
       Track the resulting duration and the offset at which the chapter starts
       inside the final book.
    3. Concat all chapter WAVs with inter-chapter silence to a single book WAV.
    4. Loudness-normalize to a target LUFS (single-pass loudnorm — adequate for
       audiobook delivery; two-pass would gain ~0.5 LU accuracy at 2× wall cost).
    5. Encode AAC + mux as .m4b, embedding chapter markers via ffmetadata so
       audiobook players show a per-chapter TOC.
    6. Write `<title>.timings.json` — the layout contract Stage 6,
       scenario-sync and `lnvox sample` read back (see MixLayout).

All real work is delegated to system ffmpeg — no in-process audio decode.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lnvox.ingest.text import Chapter
from lnvox.tts.schema import ChapterAudio


SAMPLE_RATE = 48000


@dataclass
class _ChapterTiming:
    chapter_id: str
    title: str
    start_seconds: float
    end_seconds: float

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run an ffmpeg/ffprobe command, raising with stderr on failure.

    `subprocess.run(check=True)` swallows stderr into the exception's
    `.stderr` attribute but the default repr just shows the exit code,
    which makes ffmpeg failures opaque. We rewrite the error message
    to include stderr inline so the traceback is actually informative.
    """
    try:
        # Explicit UTF-8: ffmpeg writes UTF-8 (file names, tags), and the
        # locale default on Windows (cp1252) can't decode it.
        return subprocess.run(
            cmd, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip()
        # Trim noise — keep the last ~40 lines which usually contain the
        # actual error message.
        if stderr_tail:
            lines = stderr_tail.splitlines()
            stderr_tail = "\n".join(lines[-40:])
        raise RuntimeError(
            f"{cmd[0]} failed (exit {e.returncode}).\n"
            f"command: {' '.join(cmd)}\n"
            f"--- stderr tail ---\n{stderr_tail}"
        ) from e


def _probe_duration(path: Path) -> float:
    """Return the duration in seconds of an audio file (via ffprobe)."""
    out = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    return float(out.stdout.strip())


def _make_silence(path: Path, seconds: float) -> None:
    """Write a stereo silence WAV at SAMPLE_RATE if it doesn't already exist."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i",
        f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}",
        "-t", f"{seconds:.3f}", "-c:a", "pcm_s16le", str(path),
    ])


def _concat_list(items: list[Path], list_path: Path) -> None:
    """Write a concat-demuxer list file (paths must be absolute)."""
    lines = []
    for p in items:
        ap = p.resolve()
        # ffmpeg concat-list path quoting: wrap in single quotes, escape singles.
        escaped = str(ap).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _concat_to_wav(items: list[Path], output: Path, work_dir: Path) -> None:
    """Concat a sequence of audio files into one WAV via the concat demuxer."""
    list_path = work_dir / f"{output.stem}_list.txt"
    _concat_list(items, list_path)
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2",
        str(output),
    ])


def _build_chapter_concat_items(
    chapter: ChapterAudio,
    beats_root: Path,
    silence_intra: Path,
    silence_inter_scene: Path,
    silence_lead: Path | None = None,
) -> list[Path]:
    """Order beats with the appropriate silence between them.

    `silence_lead`, when given, is prepended to EVERY beat (including the
    chapter's first): scenario mode's per-line lead-in so the reader app's
    ~0.3 s playback poll always lands in silence before speech (DESIGN.md
    §17.4). Books don't pass it.
    """
    items: list[Path] = []
    prev_scene: str | None = None
    for beat in chapter.beats:
        wav = beats_root / beat.wav_path
        if not wav.exists():
            # Some manifests store paths relative to the book artifact dir
            # rather than the project root; try the alternative.
            alt = beats_root.parent / beat.wav_path
            if alt.exists():
                wav = alt
            else:
                raise FileNotFoundError(
                    f"Beat WAV not found at {wav} (or {alt})"
                )
        if items:
            pad = silence_inter_scene if beat.scene_id != prev_scene else silence_intra
            items.append(pad)
        if silence_lead is not None:
            items.append(silence_lead)
        items.append(wav)
        prev_scene = beat.scene_id
    return items


def _loudnorm(input_wav: Path, output_wav: Path, target_lufs: float) -> None:
    """Single-pass loudness normalization to `target_lufs` LUFS."""
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_wav),
        "-af", f"loudnorm=I={target_lufs}:LRA=11:TP=-2",
        "-c:a", "pcm_s16le", "-ar", str(SAMPLE_RATE), "-ac", "2",
        str(output_wav),
    ])


def _build_ffmetadata(
    timings: list[_ChapterTiming], book_title: str, work_dir: Path
) -> Path:
    """Generate an ffmetadata file describing the book's chapter markers."""
    lines = [";FFMETADATA1", f"title={book_title}"]
    for t in timings:
        start_ms = int(round(t.start_seconds * 1000))
        end_ms = int(round(t.end_seconds * 1000))
        lines.append("")
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={t.title}")
    path = work_dir / "ffmetadata.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def resolve_book_images(
    book_dir: Path,
    novel_dir: Path,
    *,
    cover: Path | None = None,
    images_dir: Path | None = None,
    log=None,
) -> tuple[Path | None, list[Path]]:
    """`(cover_image, extra_images)` exactly as Stage 5 will embed them.

    The single source of truth for which pictures an m4b carries and in what
    order. ln-reader matches a sync-manifest image to an embedded picture by
    POSITION (MP4's `covr` atom keeps no per-picture name), so Stage 6 must
    reproduce this order exactly. It reads the list s5 recorded in the
    timings sidecar (`embedded_images`, which honours `--cover` /
    `--images-dir`) and only calls this, without overrides, for a sidecar
    that predates that field.
    """
    say = log or (lambda _m: None)

    cover_image = cover
    if cover_image is None:
        book_meta_path = book_dir / "00_book_meta.json"
        if book_meta_path.exists():
            book_meta = json.loads(book_meta_path.read_text(encoding="utf-8"))
            if book_meta.get("cover_image"):
                cover_image = Path(book_meta["cover_image"])
                if not cover_image.exists():
                    say(
                        f"[yellow]Cover image referenced by 00_book_meta.json "
                        f"is missing: {cover_image} — skipping[/]"
                    )
                    cover_image = None

    resolved_images_dir = images_dir if images_dir else (novel_dir / "images")
    extra_images: list[Path] = []
    if resolved_images_dir.exists():
        # Prefer the spine-ordered list from `.epub_meta.json` — the order a
        # reader meets the illustrations — over a directory listing, which
        # sorts `Insert10` before `Insert2`.
        meta_path = novel_dir / ".epub_meta.json"
        ordered_paths: list[Path] = []
        if images_dir is None and meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            for rel in meta.get("images", []) or []:
                candidate = novel_dir / rel
                if candidate.exists():
                    ordered_paths.append(candidate)
            if ordered_paths:
                say(
                    f"[dim]Using spine-ordered image list from {meta_path.name} "
                    f"({len(ordered_paths)} image(s))[/]"
                )
        extra_images = ordered_paths or collect_images(resolved_images_dir)
        if extra_images and cover_image is None:
            covers = [q for q in extra_images if q.stem.lower() in ("cover", "front")]
            if covers:
                cover_image = covers[0]
                extra_images = [q for q in extra_images if q != cover_image]
                say(f"[dim]Auto-promoted cover: {cover_image}[/]")
    return cover_image, extra_images


def embedded_image_order(
    cover_image: Path | None, images: list[Path] | None
) -> list[Path]:
    """The pictures `_encode_m4b` embeds, in stream order: cover first, then
    the rest, deduplicated, missing files skipped. Mirrors its loop exactly."""
    ordered: list[Path] = []
    seen: set[Path] = set()
    if cover_image and cover_image.exists():
        ordered.append(cover_image)
        seen.add(cover_image.resolve())
    for img in images or []:
        if not img.exists():
            continue
        rp = img.resolve()
        if rp in seen:
            continue
        ordered.append(img)
        seen.add(rp)
    return ordered


def collect_images(images_dir: Path) -> list[Path]:
    """Discover image files in `images_dir`, ordered with `cover.*` first.

    Convention:
        cover.jpg / cover.png / front.* → primary cover (first attached_pic)
        Everything else                  → sorted alphabetically afterwards
    """
    if not images_dir.exists() or not images_dir.is_dir():
        return []
    files = sorted(
        p for p in images_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_EXTS
    )
    cover = [p for p in files if p.stem.lower() in ("cover", "front")]
    rest = [p for p in files if p not in cover]
    return cover + rest


def _encode_m4b(
    input_wav: Path,
    metadata_path: Path,
    output_m4b: Path,
    bitrate_kbps: int,
    cover_image: Path | None = None,
    images: list[Path] | None = None,
) -> None:
    """Encode AAC and mux into an .m4b with chapter markers + embedded images.

    Each image becomes its own video stream marked `disposition: attached_pic`
    with a `title` tag equal to the image filename stem so external tooling
    can re-associate streams with sources (e.g. a custom gallery extractor).

    `cover_image` is a backward-compat alias for `images=[cover_image]`. If
    both are passed, `cover_image` is prepended to `images` (deduplicated).
    """
    # Cover first, then the rest — the order ln-reader indexes by (see
    # embedded_image_order / resolve_book_images).
    ordered = embedded_image_order(cover_image, images)

    cmd: list[str] = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_wav),
        "-i", str(metadata_path),
    ]
    for img in ordered:
        cmd.extend(["-i", str(img)])

    # Map audio + each image stream.
    cmd.extend(["-map", "0:a"])
    # ffmpeg input index for images starts at 2 (0=wav, 1=metadata).
    for idx, _ in enumerate(ordered):
        cmd.extend(["-map", f"{idx + 2}:v"])

    cmd.extend([
        "-map_metadata", "1",
        "-c:a", "aac", "-b:a", f"{bitrate_kbps}k",
    ])
    if ordered:
        cmd.extend(["-c:v", "copy"])
        for out_idx, img in enumerate(ordered):
            cmd.extend([f"-disposition:v:{out_idx}", "attached_pic"])
            # Title metadata = filename stem (cover, 01, illustration-3, …)
            cmd.extend([f"-metadata:s:v:{out_idx}", f"title={img.stem}"])

    cmd.extend([
        "-movflags", "+faststart",
        "-f", "mp4",
        str(output_m4b),
    ])
    _run(cmd)


# ---- the timings sidecar: s5's layout contract --------------------------------

TIMINGS_SUFFIX = ".timings.json"


_WINDOWS_FORBIDDEN = str.maketrans({c: "_" for c in '<>:"|?*'})


def safe_filename(book_title: str) -> str:
    """The m4b / sidecar file stem for `book_title` (`/` and `\\` → `_`).

    On Windows, also the characters NTFS forbids (`<>:"|?*` → `_`) and
    trailing dots/spaces, which Windows strips silently. A `:` matters most:
    "Title: Subtitle.m4b" would land in an alternate data stream of a file
    named "Title". Windows-only so names (and the sidecar lookups keyed on
    them) don't change for books already mixed elsewhere.
    """
    stem = book_title.replace("/", "_").replace("\\", "_")
    if sys.platform == "win32":
        stem = stem.translate(_WINDOWS_FORBIDDEN).rstrip(". ") or "_"
    return stem


@dataclass
class MixLayout:
    """One Stage-5 run's layout, read back from its `<title>.timings.json`.

    Stage 6 and scenario-sync rebuild beat times from the manifests — they
    never probe audio — so they must replay s5's layout exactly, and the
    sample builder must slice the m4b those times describe. They used to take
    the layout from their own flags, kept in step with the s5 run by hand: an
    s5 `--lead-in`, a `--chapters` subset or a `--cover`/`--images-dir`
    override silently skewed every timing or picture slot downstream. The
    sidecar records what s5 actually did, so agreement is the default.

    `audio_dir` and `embedded_images` are None for a sidecar written before
    they were recorded; callers then fall back to their old behaviour.
    """

    sidecar: Path
    m4b: Path
    book_title: str
    intra_silence: float
    inter_scene_silence: float
    inter_chapter_silence: float
    lead_in_silence: float
    # Chapter ids in mix order, and each one's probed (start, end) in the m4b.
    chapter_ids: list[str]
    chapter_bounds: dict[str, tuple[float, float]]
    audio_dir: str | None = None
    embedded_images: list[Path] | None = None


def read_mix_layout(sidecar: Path) -> MixLayout:
    """Parse a `.timings.json` sidecar (any version) into a MixLayout."""
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    stem = sidecar.name[: -len(TIMINGS_SUFFIX)]
    chapters = data.get("chapters") or []
    images = data.get("embedded_images")
    return MixLayout(
        sidecar=sidecar,
        m4b=sidecar.parent / (data.get("m4b") or f"{stem}.m4b"),
        book_title=data.get("book_title", stem),
        intra_silence=float(data.get("intra_silence", 0.25)),
        inter_scene_silence=float(data.get("inter_scene_silence", 1.0)),
        inter_chapter_silence=float(data.get("inter_chapter_silence", 2.0)),
        # Sidecars from before scenario mode have no lead-in: it was always 0.
        lead_in_silence=float(data.get("lead_in_silence", 0.0)),
        chapter_ids=[c["chapter_id"] for c in chapters],
        chapter_bounds={
            c["chapter_id"]: (float(c["start_seconds"]), float(c["end_seconds"]))
            for c in chapters
        },
        audio_dir=data.get("audio_dir"),
        embedded_images=[Path(p) for p in images] if images is not None else None,
    )


def find_mix_sidecar(final_dir: Path, title: str | None = None) -> Path | None:
    """The sidecar of the mix downstream stages should follow, or None.

    With `title` (an `lnvox s5 --title`), exactly `<title>.timings.json`.
    Without, the newest sidecar in `final_dir` — the last successful mix, so a
    stale m4b left behind by an earlier `--title` is not picked. A sidecar is
    only written once its m4b is fully encoded, which is why the sidecar, not
    the m4b, is what gets selected.
    """
    if title is not None:
        path = final_dir / f"{safe_filename(title)}{TIMINGS_SUFFIX}"
        return path if path.exists() else None
    if not final_dir.is_dir():
        return None
    sidecars = list(final_dir.glob(f"*{TIMINGS_SUFFIX}"))
    return max(sidecars, key=lambda p: p.stat().st_mtime) if sidecars else None


def load_mix_layout(final_dir: Path, title: str | None = None) -> MixLayout | None:
    """`read_mix_layout(find_mix_sidecar(...))` for the sync stages.

    Returns None when `final_dir` holds no sidecar at all (nothing mixed yet),
    so the caller falls back to its flag defaults. An explicit `title` with no
    sidecar raises FileNotFoundError instead of quietly following another mix.
    """
    sidecar = find_mix_sidecar(final_dir, title)
    if sidecar is not None:
        return read_mix_layout(sidecar)
    if title is None:
        return None
    have = sorted(
        p.name[: -len(TIMINGS_SUFFIX)] for p in final_dir.glob(f"*{TIMINGS_SUFFIX}")
    ) if final_dir.is_dir() else []
    raise FileNotFoundError(
        f"No mix titled {title!r}: "
        f"{final_dir / (safe_filename(title) + TIMINGS_SUFFIX)} does not exist"
        + (f" (mixes here: {', '.join(have)})." if have else " — run `lnvox s5` first.")
    )


def mix(
    *,
    chapters_audio: list[ChapterAudio],
    chapter_titles: dict[str, str],
    book_title: str,
    beats_root: Path,
    output_dir: Path,
    work_dir: Path | None = None,
    intra_silence: float = 0.25,
    inter_scene_silence: float = 1.0,
    inter_chapter_silence: float = 2.0,
    lead_in_silence: float = 0.0,
    target_lufs: float = -18.0,
    aac_kbps: int = 96,
    cover_image: Path | None = None,
    images: list[Path] | None = None,
    audio_dir: str | None = None,
    keep_work: bool = False,
    progress: Callable[[str], None] = print,
) -> Path:
    """Mix the entire book into a single .m4b. Returns the output path.

    `audio_dir` names the render directory the manifests came from
    (`05_audio` / `05_audio_v2`); it is only recorded in the sidecar, so
    Stage 6 can refuse to time a mix it has no per-beat manifests for.

    The default work dir (`output_dir/_work`) holds several full-length WAVs
    and is removed after a successful mix unless `keep_work`; on failure it is
    left for debugging. An explicit `work_dir` belongs to the caller and is
    never removed.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    owns_work_dir = work_dir is None
    work_dir = work_dir or output_dir / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)

    silence_intra = work_dir / f"sil_intra_{int(intra_silence * 1000)}ms.wav"
    silence_scene = work_dir / f"sil_scene_{int(inter_scene_silence * 1000)}ms.wav"
    silence_chapter = work_dir / f"sil_chapter_{int(inter_chapter_silence * 1000)}ms.wav"
    progress("Generating silence pads…")
    _make_silence(silence_intra, intra_silence)
    _make_silence(silence_scene, inter_scene_silence)
    _make_silence(silence_chapter, inter_chapter_silence)
    silence_lead: Path | None = None
    if lead_in_silence > 0:
        silence_lead = work_dir / f"sil_lead_{int(lead_in_silence * 1000)}ms.wav"
        _make_silence(silence_lead, lead_in_silence)

    progress("Concatenating per-chapter audio…")
    chapter_wavs: list[Path] = []
    timings: list[_ChapterTiming] = []
    cursor = 0.0
    for i, ch in enumerate(chapters_audio):
        ch_items = _build_chapter_concat_items(
            ch, beats_root, silence_intra, silence_scene, silence_lead
        )
        ch_wav = work_dir / f"chapter_{ch.chapter_id}.wav"
        _concat_to_wav(ch_items, ch_wav, work_dir)
        dur = _probe_duration(ch_wav)
        timings.append(
            _ChapterTiming(
                chapter_id=ch.chapter_id,
                title=chapter_titles.get(ch.chapter_id, ch.chapter_id),
                start_seconds=cursor,
                end_seconds=cursor + dur,
            )
        )
        chapter_wavs.append(ch_wav)
        cursor += dur
        if i < len(chapters_audio) - 1:
            cursor += inter_chapter_silence
        progress(f"  ✓ chapter {ch.chapter_id}: {dur:.1f}s")

    progress("Concatenating all chapters into book WAV…")
    book_items: list[Path] = []
    for i, w in enumerate(chapter_wavs):
        if i > 0:
            book_items.append(silence_chapter)
        book_items.append(w)
    book_wav = work_dir / "book_raw.wav"
    _concat_to_wav(book_items, book_wav, work_dir)
    total_duration = _probe_duration(book_wav)
    progress(f"  raw book duration: {total_duration:.1f}s ({total_duration/3600:.2f}h)")

    progress(f"Loudness-normalizing to {target_lufs} LUFS…")
    normalized_wav = work_dir / "book_normalized.wav"
    _loudnorm(book_wav, normalized_wav, target_lufs)

    progress("Building chapter metadata…")
    meta_path = _build_ffmetadata(timings, book_title, work_dir)

    # `book_title` is used BOTH as the m4b's embedded title metadata AND as
    # the output filename. Slashes (e.g. "level99/volume-01") are valid in
    # the metadata but treated as path separators by ffmpeg — sanitize them
    # for filenames so the output lands directly in `output_dir/`.
    stem = safe_filename(book_title)
    output_m4b = output_dir / f"{stem}.m4b"
    embedded = embedded_image_order(cover_image, images)
    if embedded:
        progress(
            f"Encoding AAC @ {aac_kbps}kbps + embedding {len(embedded)} image(s) → {output_m4b.name}…"
        )
    else:
        progress(f"Encoding AAC @ {aac_kbps}kbps → {output_m4b.name}…")
    _encode_m4b(
        normalized_wav,
        meta_path,
        output_m4b,
        aac_kbps,
        cover_image=cover_image,
        images=images,
    )

    # The layout contract (MixLayout): everything Stage 6 / scenario-sync /
    # `lnvox sample` need to reproduce this mix — pads, lead-in, the chapter
    # list actually mixed and the pictures in embed order. Written only after
    # the encode succeeded, so a sidecar always describes a complete m4b.
    sidecar = output_dir / f"{stem}{TIMINGS_SUFFIX}"
    sidecar.write_text(
        json.dumps(
            {
                "book_title": book_title,
                "m4b": output_m4b.name,
                "audio_dir": audio_dir,
                "total_duration_seconds": total_duration,
                "target_lufs": target_lufs,
                "intra_silence": intra_silence,
                "inter_scene_silence": inter_scene_silence,
                "inter_chapter_silence": inter_chapter_silence,
                "lead_in_silence": lead_in_silence,
                "embedded_images": [str(p) for p in embedded],
                "chapters": [
                    {
                        "chapter_id": t.chapter_id,
                        "title": t.title,
                        "start_seconds": round(t.start_seconds, 3),
                        "end_seconds": round(t.end_seconds, 3),
                        "duration_seconds": round(t.duration_seconds, 3),
                    }
                    for t in timings
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Nothing reads _work/ across runs: every chapter WAV, the book WAVs and
    # the metadata are rebuilt each time, and the silence pads it would reuse
    # take milliseconds to regenerate. It holds ~3 uncompressed copies of the
    # book (~0.7 GB each per hour of audio), so drop it once the m4b is safe.
    if owns_work_dir and not keep_work:
        shutil.rmtree(work_dir, ignore_errors=True)

    progress(f"Done. Final m4b: {output_m4b}")
    return output_m4b
