"""Tests for the edge-silence trim (DESIGN.md §2.6).

Runnable directly (`python tests/test_trim.py`) or under pytest.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import soundfile as sf

from lnvox.tts.trim import DEFAULT_PAD_MS, trim_array, trim_file

SR = 48_000


def _tone_with_silence(lead_s: float, tone_s: float, trail_s: float, stereo: bool = False):
    t = np.arange(int(SR * tone_s)) / SR
    tone = 0.3 * np.sin(2 * np.pi * 440 * t)
    audio = np.concatenate(
        [np.zeros(int(SR * lead_s)), tone, np.zeros(int(SR * trail_s))]
    ).astype(np.float64)
    if stereo:
        audio = np.stack([audio, audio], axis=1)
    return audio


def test_trim_array_removes_edges_keeps_pad():
    audio = _tone_with_silence(2.0, 1.0, 1.5)
    trimmed, lead, trail = trim_array(audio, SR)
    pad = DEFAULT_PAD_MS / 1000
    assert abs(lead - (2.0 - pad)) < 0.05, lead
    assert abs(trail - (1.5 - pad)) < 0.05, trail
    assert abs(len(trimmed) / SR - (1.0 + 2 * pad)) < 0.1


def test_trim_array_stereo_and_all_silence():
    trimmed, lead, trail = trim_array(_tone_with_silence(1.0, 0.5, 1.0, stereo=True), SR)
    assert trimmed.ndim == 2 and trimmed.shape[1] == 2
    assert lead > 0.8 and trail > 0.8
    # All-silent input is returned untouched — upstream failures stay audible.
    silent = np.zeros(SR)
    trimmed, lead, trail = trim_array(silent, SR)
    assert len(trimmed) == SR and lead == 0.0 and trail == 0.0


def test_trim_file_in_place_idempotent_and_kill_switch():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "beat.wav"
        sf.write(str(path), _tone_with_silence(2.0, 1.0, 1.5, stereo=True), SR, subtype="PCM_16")

        removed = trim_file(path)
        assert removed > 3.0, removed
        info = sf.info(str(path))
        assert info.subtype == "PCM_16"  # format preserved
        assert abs(info.frames / SR - 1.2) < 0.15

        # Idempotent: nothing meaningful left to remove.
        assert trim_file(path) == 0.0

        # Kill switch: LNVOX_S4_NO_TRIM=1 makes it a no-op.
        path2 = Path(tmp) / "beat2.wav"
        sf.write(str(path2), _tone_with_silence(2.0, 1.0, 1.5), SR, subtype="PCM_16")
        os.environ["LNVOX_S4_NO_TRIM"] = "1"
        try:
            assert trim_file(path2) == 0.0
            assert abs(sf.info(str(path2)).frames / SR - 4.5) < 0.05
        finally:
            del os.environ["LNVOX_S4_NO_TRIM"]

        # Near-clean file (tiny edges) is left untouched.
        path3 = Path(tmp) / "beat3.wav"
        sf.write(str(path3), _tone_with_silence(0.11, 1.0, 0.11), SR, subtype="PCM_16")
        assert trim_file(path3) == 0.0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()
