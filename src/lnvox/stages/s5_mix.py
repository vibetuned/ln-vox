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

All real work is delegated to system ffmpeg — no in-process audio decode.
"""

from __future__ import annotations

import json
import subprocess
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
        return subprocess.run(cmd, check=True, capture_output=True, text=True)
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
) -> list[Path]:
    """Order beats with the appropriate silence between them."""
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
    # Build the ordered image list. Cover first, then rest.
    ordered: list[Path] = []
    seen: set[Path] = set()
    if cover_image and cover_image.exists():
        rp = cover_image.resolve()
        ordered.append(cover_image)
        seen.add(rp)
    for img in images or []:
        if not img.exists():
            continue
        rp = img.resolve()
        if rp in seen:
            continue
        ordered.append(img)
        seen.add(rp)

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
    target_lufs: float = -18.0,
    aac_kbps: int = 96,
    cover_image: Path | None = None,
    images: list[Path] | None = None,
    progress: Callable[[str], None] = print,
) -> Path:
    """Mix the entire book into a single .m4b. Returns the output path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir or output_dir / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)

    silence_intra = work_dir / f"sil_intra_{int(intra_silence * 1000)}ms.wav"
    silence_scene = work_dir / f"sil_scene_{int(inter_scene_silence * 1000)}ms.wav"
    silence_chapter = work_dir / f"sil_chapter_{int(inter_chapter_silence * 1000)}ms.wav"
    progress("Generating silence pads…")
    _make_silence(silence_intra, intra_silence)
    _make_silence(silence_scene, inter_scene_silence)
    _make_silence(silence_chapter, inter_chapter_silence)

    progress("Concatenating per-chapter audio…")
    chapter_wavs: list[Path] = []
    timings: list[_ChapterTiming] = []
    cursor = 0.0
    for i, ch in enumerate(chapters_audio):
        ch_items = _build_chapter_concat_items(
            ch, beats_root, silence_intra, silence_scene
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
    safe_filename = book_title.replace("/", "_").replace("\\", "_")
    output_m4b = output_dir / f"{safe_filename}.m4b"
    image_count = (1 if cover_image and cover_image.exists() else 0) + len(
        [p for p in (images or []) if p.exists()]
    )
    if image_count:
        progress(
            f"Encoding AAC @ {aac_kbps}kbps + embedding {image_count} image(s) → {output_m4b.name}…"
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

    # Persist a sidecar timing manifest for debugging / future re-encode.
    sidecar = output_dir / f"{safe_filename}.timings.json"
    sidecar.write_text(
        json.dumps(
            {
                "book_title": book_title,
                "total_duration_seconds": total_duration,
                "target_lufs": target_lufs,
                "intra_silence": intra_silence,
                "inter_scene_silence": inter_scene_silence,
                "inter_chapter_silence": inter_chapter_silence,
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

    progress(f"Done. Final m4b: {output_m4b}")
    return output_m4b
