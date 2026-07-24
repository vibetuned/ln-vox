"""Edge-silence trim for rendered TTS WAVs (DESIGN.md §2.6).

Dramabox sizes its latent from a duration ESTIMATE (`duration_multiplier`);
the overshoot renders as silence, mostly leading. Measured on real artifacts
(2026-07-24, −40 dBFS threshold): a rendered play had a median 2.2 s of
leading silence — 70% of lines over 1 s, 29% of all rendered audio was edge
silence; a novel carried ~15%. Long narration beats hide it; play dialogue
doesn't.

The fix is deterministic DSP, engine-agnostic and idempotent: a 10 ms RMS
envelope, an absolute threshold, and a keep-pad at both edges. It runs where
a WAV lands in `05_audio/` (and upgrades the content cache in place), so the
durations written to manifests — and therefore s5 mixing, s6 sync, and the
§17.4 scenario sync — are always measured on trimmed audio.

Kill switch: `LNVOX_S4_NO_TRIM=1` (set by `lnvox s4 --no-trim`) makes
`trim_file` a no-op — the escape hatch if the threshold ever eats a soft
lead-in on some voice.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_THRESHOLD_DB = -40.0
DEFAULT_PAD_MS = 100.0
_WIN_MS = 10.0
# Below this much removable silence, leave the file untouched — avoids
# rewriting virtually every WAV to shave a few samples.
_MIN_TRIM_SECONDS = 0.05


def trim_array(
    audio,
    sr: int,
    *,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    pad_ms: float = DEFAULT_PAD_MS,
):
    """Return (trimmed_audio, lead_removed_s, trail_removed_s).

    `audio` is float samples, shape (n,) or (n, channels). A file with no
    samples above the threshold is returned UNTOUCHED — an all-silent render
    is an upstream failure that must stay audible, not be masked by emptying
    the file.
    """
    import numpy as np

    mono = audio.mean(axis=1) if audio.ndim == 2 else audio
    win = max(1, int(sr * _WIN_MS / 1000))
    env = np.sqrt(
        np.convolve(mono.astype(np.float64) ** 2, np.ones(win) / win, mode="same")
    )
    above = np.nonzero(env > 10 ** (threshold_db / 20))[0]
    if len(above) == 0:
        return audio, 0.0, 0.0
    pad = int(sr * pad_ms / 1000)
    start = max(0, int(above[0]) - pad)
    end = min(len(mono), int(above[-1]) + 1 + pad)
    return audio[start:end], start / sr, (len(mono) - end) / sr


def trim_file(
    path: Path,
    *,
    threshold_db: float = DEFAULT_THRESHOLD_DB,
    pad_ms: float = DEFAULT_PAD_MS,
) -> float:
    """Trim `path` in place, preserving its PCM subtype. Returns seconds removed.

    No-op (returns 0.0) when less than ~50 ms would be removed, or when
    `LNVOX_S4_NO_TRIM=1`.
    """
    if os.environ.get("LNVOX_S4_NO_TRIM") == "1":
        return 0.0
    import soundfile as sf

    data, sr = sf.read(str(path))
    trimmed, lead, trail = trim_array(
        data, sr, threshold_db=threshold_db, pad_ms=pad_ms
    )
    removed = lead + trail
    if removed < _MIN_TRIM_SECONDS:
        return 0.0
    subtype = sf.info(str(path)).subtype
    sf.write(str(path), trimmed, sr, subtype=subtype)
    return removed
