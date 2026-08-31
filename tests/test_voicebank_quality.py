"""Quality-ranked seeding: probe, score, quotas (DESIGN.md §19)."""

import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")

from lnvox.voices.common_voice import (  # noqa: E402
    _bucket_quotas,
    probe_recording_quality,
    quality_score,
)

SR = 24000


def _write(path, y, sr=SR):
    soundfile.write(str(path), y.astype("float32"), sr, subtype="PCM_16")
    return path


def _voiced(seconds=3.0, f0=120.0, sr=SR, bandwidth=None):
    """A crude buzzy 'voice': harmonic stack, optionally low-passed."""
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    y = np.zeros_like(t)
    h = 1
    while h * f0 < (bandwidth or sr / 2):
        y += np.sin(2 * np.pi * f0 * h * t) / h
        h += 1
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)  # syllable-rate envelope
    return 0.6 * y / np.max(np.abs(y)) * env


def test_probe_rejects_too_short(tmp_path):
    p = _write(tmp_path / "a.wav", _voiced(0.4))
    assert probe_recording_quality(p) is None


def test_probe_rejects_unreadable(tmp_path):
    bad = tmp_path / "nope.wav"
    bad.write_bytes(b"not audio")
    assert probe_recording_quality(bad) is None


def test_bandwidth_separates_full_band_from_phone_capture(tmp_path):
    wide = probe_recording_quality(_write(tmp_path / "w.wav", _voiced()))
    narrow = probe_recording_quality(
        _write(tmp_path / "n.wav", _voiced(bandwidth=3400))
    )
    assert wide and narrow
    assert wide["bandwidth"] > narrow["bandwidth"] * 1.5
    assert quality_score(wide) > quality_score(narrow)


def test_noise_raises_floor_and_lowers_score(tmp_path):
    clean = _voiced()
    rng = np.random.default_rng(0)
    noisy = clean + rng.normal(0, 0.05, clean.shape)
    a = probe_recording_quality(_write(tmp_path / "c.wav", clean))
    b = probe_recording_quality(_write(tmp_path / "n.wav", noisy))
    assert a and b
    assert a["snr"] > b["snr"]
    assert quality_score(a) > quality_score(b)


def test_inserted_digital_silence_does_not_inflate_snr(tmp_path):
    """§19.2: the seeder pads utterances with true zeros; if those frames are
    read as the noise floor every clip scores an absurd SNR."""
    v = _voiced(2.0)
    gap = np.zeros(int(SR * 0.25), dtype=v.dtype)
    padded = np.concatenate([v, gap, v, gap, v])
    m = probe_recording_quality(_write(tmp_path / "p.wav", padded))
    assert m is not None
    assert m["snr"] < 80, f"digital silence leaked into the floor: {m['snr']}"


def test_clipping_is_penalised():
    base = {"bandwidth": 11000, "snr": 40, "rumble": 0.0, "hiss": 0.0, "clip": 0.0}
    assert quality_score({**base, "clip": 0.01}) < quality_score(base)


def test_rumble_and_hiss_are_penalised():
    base = {"bandwidth": 11000, "snr": 40, "rumble": 0.0, "hiss": 0.0, "clip": 0.0}
    assert quality_score({**base, "rumble": 0.2}) < quality_score(base)
    assert quality_score({**base, "hiss": 0.8}) < quality_score(base)


def _pool(**counts):
    out = []
    for key, n in counts.items():
        gender, age = key.split("__")
        out += [{"gender": gender, "age_band": age}] * n
    return out


def test_quotas_sum_to_the_bank_size():
    q = _bucket_quotas(_pool(male__young_adult=900, female__adult=300,
                             female__elder=60, male__teen=200), 400)
    assert sum(q.values()) == 400


def test_quota_floor_protects_rare_buckets():
    """Without a floor, the male/young-adult majority takes nearly everything."""
    q = _bucket_quotas(_pool(male__young_adult=2000, female__elder=40), 400)
    assert q[("female", "elder")] >= 400 // 20
    assert sum(q.values()) == 400


def test_quota_never_exceeds_available_candidates():
    q = _bucket_quotas(_pool(male__young_adult=1000, female__elder=3), 400)
    assert q[("female", "elder")] == 3
    assert sum(q.values()) <= 400


def test_quotas_empty_pool():
    assert _bucket_quotas([], 400) == {}
