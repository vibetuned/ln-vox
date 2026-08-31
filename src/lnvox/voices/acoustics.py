"""Per-clip acoustic metrics, cached alongside the voicebank (DESIGN.md §20.3).

The casting audit needs to know whether two reference clips actually sound
different. The manifest records demographics, which say nothing about that, so
we measure the clips themselves and cache the result in
``<voicebank>/acoustics.json``.

The cache is incremental and disposable: clips already indexed are skipped,
and deleting the file costs exactly one rebuild.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from lnvox.voices.common_voice import probe_recording_quality
from lnvox.voices.schema import VoiceClip

ACOUSTICS_FILE = "acoustics.json"

# Human speech f0. Wider than any real speaker so an odd clip is measured
# rather than silently dropped.
_F0_MIN, _F0_MAX = 60.0, 400.0


def semitones_apart(a: float, b: float) -> float:
    """Absolute pitch distance in semitones. 0.0 when either is unknown."""
    if not a or not b or a <= 0 or b <= 0:
        return 0.0
    return abs(12.0 * math.log2(a / b))


def clip_acoustics(path: Path) -> dict | None:
    """Recording quality plus median f0 for one clip, or None if unreadable."""
    metrics = probe_recording_quality(path)
    if metrics is None:
        return None

    import librosa
    import numpy as np

    try:
        y, sr = librosa.load(str(path), sr=None, mono=True)
        f0 = librosa.yin(y, fmin=_F0_MIN, fmax=_F0_MAX, sr=sr,
                         frame_length=2048, hop_length=512)
        voiced = f0[(f0 > _F0_MIN) & (f0 < _F0_MAX)]
        pitch = float(np.median(voiced)) if voiced.size > 10 else 0.0
    except Exception:
        pitch = 0.0

    return {**metrics, "pitch": pitch}


def load_acoustics(
    voicebank_dir: Path,
    clips: list[VoiceClip],
    *,
    progress=None,
) -> dict[str, dict]:
    """Acoustic index for `clips`, building and caching whatever is missing."""
    cache_path = voicebank_dir / ACOUSTICS_FILE
    index: dict[str, dict] = {}
    if cache_path.exists():
        try:
            index = json.loads(cache_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            index = {}

    missing = [c for c in clips if c.id not in index]
    if missing:
        if progress:
            progress(f"Indexing {len(missing)} clip(s) for acoustic audit…")
        for i, clip in enumerate(missing, start=1):
            m = clip_acoustics(voicebank_dir / clip.clip_path)
            if m is not None:
                index[clip.id] = m
            if progress and (i % 50 == 0 or i == len(missing)):
                progress(f"  indexed {i}/{len(missing)}")
        cache_path.write_text(
            json.dumps(index, indent=1, sort_keys=True), encoding="utf-8"
        )
    return index
