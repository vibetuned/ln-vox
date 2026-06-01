"""Seed the voicebank from a LOCAL Mozilla Common Voice tarball.

As of Common Voice 23.0, the dataset is distributed exclusively via the Mozilla
Data Collective rather than HuggingFace. The user downloads and extracts the
tarball themselves; this loader then reads from the extracted directory.

Expected layout after extraction (the tarball produces this structure):

    <cv_root>/
        clips/                            (MP3 files, one per utterance)
        validated.tsv                     (recommended — already QC-passed)
        train.tsv / dev.tsv / test.tsv
        invalidated.tsv / other.tsv

Where `<cv_root>` is typically `cv-corpus-XX.X-YYYY-MM-DD/<locale>/` after
extraction. Point `seed_from_common_voice()` at that directory.

This loader groups utterances by `client_id` (speaker), takes the top-voted
ones, and concatenates them into a single 10-15 second reference clip per
speaker. Demographic metadata (age / gender / accents) is preserved.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from lnvox.voices.schema import VoiceClip, Voicebank


# CV 25 age codes (per README — note the "fourties" misspelling is intentional).
_AGE_MAP: dict[str, str] = {
    "teens": "teen",
    "twenties": "young_adult",
    "thirties": "young_adult",
    "fourties": "adult",
    "fifties": "adult",
    "sixties": "elder",
    "seventies": "elder",
    "eighties": "elder",
    "nineties": "elder",
}

# CV 25 gender codes. We only retain male_masculine and female_feminine
# because the schema's voice categories are acoustic (matching is by vocal
# range and timbre) and the other codes have < 0.1% of speakers. Non-binary
# and transgender speakers span the full vocal range and can't be auto-
# categorized into the male/female matching buckets; manual assignment
# remains available for those.
_GENDER_MAP: dict[str, str] = {
    "male_masculine": "male",
    "female_feminine": "female",
}


# CV stores the `accents` column as human-readable names, not codes.
# This map converts the official names (per the README accent table) to the
# canonical short codes that the voicebank schema uses for matching.
# Multi-accent entries are PIPE-separated (e.g. "United States English|wolof").
_CV_ACCENT_NAME_TO_CODE: dict[str, str] = {
    "United States English": "us",
    "England English": "england",
    "India and South Asia (India, Pakistan, Sri Lanka)": "indian",
    "Canadian English": "canada",
    "Australian English": "australia",
    "Scottish English": "scotland",
    "Southern African (South Africa, Zimbabwe, Namibia)": "african",
    "New Zealand English": "newzealand",
    "Irish English": "ireland",
    "Filipino": "philippines",
    "Hong Kong English": "hongkong",
    "Singaporean English": "singapore",
    "Malaysian English": "malaysia",
    "Welsh English": "wales",
    "West Indies and Bermuda (Bahamas, Bermuda, Jamaica, Trinidad)": "bermuda",
    "South Atlantic (Falkland Islands, Saint Helena)": "southatlandtic",
}


def _normalize_accent(s: str | None) -> str:
    """Convert CV's `accents` column to a canonical accent code.

    - Multiple accents are pipe-separated; we take the first.
    - Map the human-readable name to a CV short code (us, england, …).
    - Any unrecognised value becomes "other".
    - Missing / empty becomes "any" (no accent declared).
    """
    if not s:
        return "any"
    first = s.split("|")[0].strip()
    if not first:
        return "any"
    return _CV_ACCENT_NAME_TO_CODE.get(first, "other")


def _eligible(row: dict) -> bool:
    age = (row.get("age") or "").strip()
    gender = (row.get("gender") or "").strip()
    if not age or not gender:
        return False
    if gender not in _GENDER_MAP or age not in _AGE_MAP:
        return False
    try:
        up = int(row.get("up_votes") or 0)
        down = int(row.get("down_votes") or 0)
    except (TypeError, ValueError):
        return False
    return up >= 2 and down == 0


def _audio_libs():
    """Import the optional voice deps, with one friendly error if missing."""
    try:
        import librosa
        import numpy as np
        import soundfile as sf
    except ImportError as e:
        raise RuntimeError(
            "Voice dependencies missing. Install with: `uv sync --extra voice` "
            "(librosa needs system ffmpeg for MP3 decoding).\n"
            f"(import error: {e})"
        ) from e
    return np, sf, librosa


def build_speaker_clip(
    speaker_id: str,
    rows: list[dict],
    *,
    clips_src: Path,
    voicebank_dir: Path,
    tsv_name: str = "manual",
    target_seconds: float = 12.0,
    min_seconds: float = 8.0,
    output_sr: int = 24000,
    on_missing=None,
) -> VoiceClip | None:
    """Concatenate one speaker's top-voted utterances into a reference clip.

    Writes ``voicebank_dir/clips/<id>.wav`` (mono ``output_sr``) and returns the
    corresponding :class:`VoiceClip`, or ``None`` when the speaker yields less
    than ``min_seconds`` of clean speech.

    This is the single source of truth for what a Common Voice voicebank clip
    looks like: it backs both the bulk seeder (:func:`seed_from_common_voice`)
    and the Voicebank Studio's "promote" / "preview merged" actions (DESIGN
    §12). Pass ``voicebank_dir`` a temp directory to build a throwaway preview
    without touching the real voicebank.

    Args:
        speaker_id: Common Voice ``client_id`` of the speaker.
        rows: That speaker's eligible TSV rows (need not be pre-sorted).
        clips_src: The corpus ``clips/`` directory holding the source MP3s.
        voicebank_dir: Destination voicebank root (``clips/`` is created in it).
        tsv_name: Recorded in the clip's ``notes`` for provenance.
        on_missing: Optional zero-arg callback, invoked once per missing or
            unreadable source MP3 (used by the seeder for its skip tally).
    """
    np, sf, librosa = _audio_libs()

    rows = sorted(
        rows,
        key=lambda r: int(r.get("up_votes") or 0) - int(r.get("down_votes") or 0),
        reverse=True,
    )

    segments: list = []
    sentences: list[str] = []
    total_dur = 0.0
    first_sr: int | None = None

    for row in rows:
        mp3_path = clips_src / row["path"]
        if not mp3_path.exists():
            if on_missing:
                on_missing()
            continue
        try:
            audio, sr = librosa.load(str(mp3_path), sr=None, mono=True)
        except Exception:
            if on_missing:
                on_missing()
            continue
        if audio.size == 0:
            continue
        trimmed, _ = librosa.effects.trim(audio, top_db=30)
        if len(trimmed) < sr * 0.5:
            continue
        first_sr = first_sr or sr
        if sr != first_sr:
            trimmed = librosa.resample(trimmed, orig_sr=sr, target_sr=first_sr)
        segments.append(trimmed)
        sentence = (row.get("sentence") or "").strip()
        if sentence:
            sentences.append(sentence)
        total_dur += len(trimmed) / first_sr
        if total_dur >= target_seconds:
            break

    if total_dur < min_seconds or not segments or first_sr is None:
        return None

    silence = np.zeros(int(first_sr * 0.25), dtype=segments[0].dtype)
    parts: list = []
    for i, seg in enumerate(segments):
        if i > 0:
            parts.append(silence)
        parts.append(seg)
    merged = np.concatenate(parts)

    if first_sr != output_sr:
        merged = librosa.resample(
            merged.astype("float32"), orig_sr=first_sr, target_sr=output_sr
        )

    first_row = rows[0]
    clip_id = f"cv_{speaker_id[:12]}"
    rel_clip = Path("clips") / f"{clip_id}.wav"
    (voicebank_dir / "clips").mkdir(parents=True, exist_ok=True)
    sf.write(str(voicebank_dir / rel_clip), merged, output_sr, subtype="PCM_16")

    return VoiceClip(
        id=clip_id,
        source="common_voice",
        clip_path=str(rel_clip),
        duration_seconds=round(len(merged) / output_sr, 2),
        gender=_GENDER_MAP[(first_row.get("gender") or "").strip()],
        age_band=_AGE_MAP[(first_row.get("age") or "").strip()],
        accent=_normalize_accent(first_row.get("accents") or first_row.get("variant")),
        sample_sentences=sentences[:3],
        license="CC0",
        notes=f"Common Voice ({tsv_name}); speaker {speaker_id[:8]}…",
    )


def _detect_tsv(cv_root: Path, prefer: str) -> Path:
    """Find the best TSV file for our purpose, falling back gracefully."""
    candidates = [prefer, "validated.tsv", "train.tsv", "dev.tsv", "test.tsv"]
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        p = cv_root / name
        if p.exists():
            return p
    raise FileNotFoundError(
        f"No CV TSV file found under {cv_root}. "
        f"Expected one of: {', '.join(candidates)}"
    )


def seed_from_common_voice(
    voicebank_dir: Path,
    *,
    cv_root: Path,
    tsv_name: str = "validated.tsv",
    max_speakers: int = 200,
    target_seconds: float = 12.0,
    min_seconds: float = 8.0,
    output_sr: int = 24000,
    progress=print,
) -> Voicebank:
    """Build a Voicebank from a locally-extracted Common Voice tarball.

    Args:
        voicebank_dir: Where to write `clips/<id>.wav` and `manifest.json`.
        cv_root: Path to the extracted CV directory (must contain clips/ + TSVs).
        tsv_name: Preferred TSV file (defaults to `validated.tsv`).
        max_speakers: Hard cap on speakers to write.
        target_seconds: Stop concatenating once a speaker's clip hits this length.
        min_seconds: Drop speakers whose total clean speech falls below this.
        output_sr: Sample rate of the written mono WAV files.
        progress: Callable for status messages.
    """
    # Fail fast on missing voice deps before the long TSV scan.
    _audio_libs()

    cv_root = cv_root.expanduser().resolve()
    if not cv_root.exists() or not cv_root.is_dir():
        raise FileNotFoundError(
            f"CV root not found or not a directory: {cv_root}"
        )

    clips_src = cv_root / "clips"
    if not clips_src.exists():
        raise FileNotFoundError(
            f"Expected '{clips_src}' (the CV clips directory) but it's missing. "
            f"Is {cv_root} really an extracted Common Voice locale directory?"
        )

    tsv_path = _detect_tsv(cv_root, tsv_name)
    progress(f"Reading metadata from {tsv_path.relative_to(cv_root.parent)}")

    # ---------- pass 1: group eligible rows by speaker ----------

    by_speaker: dict[str, list[dict]] = defaultdict(list)
    total_rows = 0
    eligible_rows = 0
    with tsv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            total_rows += 1
            if not _eligible(row):
                continue
            cid = row.get("client_id")
            if not cid:
                continue
            by_speaker[cid].append(row)
            eligible_rows += 1
    progress(
        f"Scanned {total_rows:,} rows; "
        f"{eligible_rows:,} eligible across {len(by_speaker):,} speakers"
    )

    # ---------- pass 2: build a ref clip per speaker ----------

    clips_dst = voicebank_dir / "clips"
    clips_dst.mkdir(parents=True, exist_ok=True)

    out: list[VoiceClip] = []
    skipped_short = 0
    skipped_missing = 0

    # Iterate in a stable order so reruns produce the same selection.
    speaker_ids = sorted(by_speaker.keys())

    def _count_missing() -> None:
        nonlocal skipped_missing
        skipped_missing += 1

    for speaker_id in speaker_ids:
        if len(out) >= max_speakers:
            break

        clip = build_speaker_clip(
            speaker_id,
            by_speaker[speaker_id],
            clips_src=clips_src,
            voicebank_dir=voicebank_dir,
            tsv_name=tsv_path.name,
            target_seconds=target_seconds,
            min_seconds=min_seconds,
            output_sr=output_sr,
            on_missing=_count_missing,
        )
        if clip is None:
            skipped_short += 1
            continue

        out.append(clip)
        if len(out) % 10 == 0:
            progress(f"  wrote {len(out)}/{max_speakers} speaker clip(s)")

    progress(
        f"Done. {len(out)} speaker clip(s) written. "
        f"Skipped {skipped_short} for insufficient duration, "
        f"{skipped_missing} for missing/unreadable MP3s."
    )
    return Voicebank(clips=out)
