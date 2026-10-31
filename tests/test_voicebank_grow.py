"""Growing an existing voicebank: --only-new / --fill-thin (DESIGN.md §19.6).

All speakers and audio here are synthetic.
"""

import csv
import hashlib
from pathlib import Path

import numpy as np
import pytest

soundfile = pytest.importorskip("soundfile")

from lnvox.voices.common_voice import (
    clip_id_for,
    probe_recording_quality,
    seed_from_common_voice,
    water_fill,
)

FE, MY, FA = ("female", "elder"), ("male", "young_adult"), ("female", "adult")


# ---- water-filling -----------------------------------------------------------


def test_smallest_bucket_fills_first():
    alloc = water_fill({FE: 2, MY: 10}, {FE: 50, MY: 50}, 4)
    assert alloc == {FE: 4}


def test_levels_buckets_then_alternates():
    alloc = water_fill({FE: 2, FA: 3}, {FE: 50, FA: 50}, 5)
    assert alloc == {FE: 3, FA: 2}          # both end at 5


def test_a_bucket_never_takes_more_than_its_clean_candidates():
    alloc = water_fill({FE: 0, MY: 100}, {FE: 2, MY: 50}, 10)
    assert alloc[FE] == 2 and alloc[MY] == 8


def test_stops_when_every_candidate_is_spent():
    assert sum(water_fill({FE: 0}, {FE: 3}, 10).values()) == 3


def test_nothing_requested_nothing_allocated():
    assert water_fill({FE: 0}, {FE: 3}, 0) == {}


# ---- the seeder end to end ---------------------------------------------------

SR = 24000


def _voice(seconds, f0, bandwidth=None):
    t = np.linspace(0, seconds, int(SR * seconds), endpoint=False)
    y = np.zeros_like(t)
    h = 1
    while h * f0 < (bandwidth or SR / 2):
        y += np.sin(2 * np.pi * f0 * h * t) / h
        h += 1
    # Syllables separated by near-silence (~-50 dB room tone), like real speech;
    # an envelope that never dips caps the measured SNR near 20 dB.
    env = 0.003 + np.clip(np.sin(2 * np.pi * 3 * t), 0, None) ** 2
    return (0.6 * y / np.max(np.abs(y)) * env).astype("float32")


SPEAKERS = {
    # client_id (>=12 chars)   gender            age        f0   bandwidth
    "aaaaaaaaaaaa01": ("male_masculine",  "twenties", 120, None),   # already banked
    "bbbbbbbbbbbb02": ("female_feminine", "sixties",  200, None),   # clean, thin bucket
    "cccccccccccc03": ("female_feminine", "sixties",  210, 3400),   # muffled, thin bucket
    "dddddddddddd04": ("male_masculine",  "twenties", 110, None),   # clean, crowded bucket
}


def _corpus(tmp_path: Path, speakers: dict = SPEAKERS) -> Path:
    root = tmp_path / "cv"
    (root / "clips").mkdir(parents=True)
    rows = []
    for cid, (gender, age, f0, bw) in speakers.items():
        for k in range(3):                              # 3 × 3.5 s ≥ min_seconds
            name = f"{cid}_{k}.wav"
            # One person, one pitch. Pure tones at different f0s put their
            # harmonics in different bins, and the stitched clip's averaged
            # spectrum then under-reads bandwidth — a fixture artefact.
            soundfile.write(str(root / "clips" / name), _voice(3.5, f0, bw), SR)
            rows.append({"client_id": cid, "path": name, "sentence": "Invented line.",
                         "up_votes": "3", "down_votes": "0", "age": age,
                         "gender": gender, "accents": "United States English"})
    with (root / "validated.tsv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter="\t")
        w.writeheader(); w.writerows(rows)
    return root


@pytest.fixture
def bank(tmp_path):
    cv = _corpus(tmp_path)
    for cid, (_g, _a, f0, bw) in SPEAKERS.items():    # fixture sanity, not the subject
        m = probe_recording_quality(cv / "clips" / f"{cid}_0.wav")
        clean = m["bandwidth"] >= 9000 and m["snr"] >= 32
        assert clean == (bw is None), (cid, m)
    vb = tmp_path / "bank"
    (vb / "clips").mkdir(parents=True)
    existing = vb / "clips" / f"{clip_id_for('aaaaaaaaaaaa01')}.wav"
    existing.write_bytes(b"pre-existing clip bytes")
    return cv, vb, existing


def _grow(cv, vb, n, **kw):
    return seed_from_common_voice(
        vb, cv_root=cv, max_speakers=n, accents={"us"},
        exclude_ids={clip_id_for("aaaaaaaaaaaa01")},
        fill_thin_from={MY: 10, FE: 0}, progress=lambda _m: None, **kw,
    )


def test_fill_thin_adds_the_clean_voice_from_the_thinnest_bucket(bank):
    cv, vb, _ = bank
    out = _grow(cv, vb, 1)
    assert [c.id for c in out.clips] == [clip_id_for("bbbbbbbbbbbb02")]


def test_muffled_voices_are_never_admitted_even_to_a_thin_bucket(bank):
    cv, vb, _ = bank
    out = _grow(cv, vb, 10)
    ids = {c.id for c in out.clips}
    assert clip_id_for("cccccccccccc03") not in ids
    assert ids == {clip_id_for("bbbbbbbbbbbb02"), clip_id_for("dddddddddddd04")}


def test_an_existing_clip_is_never_reselected_or_rewritten(bank):
    cv, vb, existing = bank
    before = hashlib.sha1(existing.read_bytes()).digest()
    out = _grow(cv, vb, 10)
    assert clip_id_for("aaaaaaaaaaaa01") not in {c.id for c in out.clips}
    assert hashlib.sha1(existing.read_bytes()).digest() == before


def test_every_added_clip_has_its_wav_and_rejected_ones_leave_none(bank):
    cv, vb, _ = bank
    out = _grow(cv, vb, 10)
    wavs = {p.stem for p in (vb / "clips").glob("*.wav")}
    assert {c.id for c in out.clips} <= wavs
    assert clip_id_for("cccccccccccc03") not in wavs


def test_a_built_clip_that_fails_the_floor_is_replaced_from_its_own_bucket(tmp_path, monkeypatch):
    """The probe hears one utterance; the stitched clip can be worse. When the
    finished clip fails, it is deleted and the bucket's next-best voice is
    built instead — not a voice from some other bucket."""
    import lnvox.voices.common_voice as cvmod
    speakers = {**SPEAKERS, "eeeeeeeeeeee05": ("female_feminine", "sixties", 230, None)}
    cv = _corpus(tmp_path, speakers)
    vb = tmp_path / "bank"; (vb / "clips").mkdir(parents=True)
    real_probe = cvmod.probe_recording_quality
    first_choice = {}

    def probe(path):
        m = real_probe(path)
        # Fail whichever female elder gets BUILT first, whatever its score.
        if m and Path(path).parent == vb / "clips" and not first_choice:
            first_choice["id"] = Path(path).stem
            return {**m, "bandwidth": 4000.0}
        return m

    monkeypatch.setattr(cvmod, "probe_recording_quality", probe)
    out = seed_from_common_voice(
        vb, cv_root=cv, max_speakers=1, accents={"us"},
        exclude_ids={clip_id_for("aaaaaaaaaaaa01")},
        fill_thin_from={MY: 10, FE: 0}, progress=lambda _m: None,
    )
    elders = {clip_id_for("bbbbbbbbbbbb02"), clip_id_for("eeeeeeeeeeee05")}
    assert first_choice["id"] in elders
    assert [c.id for c in out.clips] == list(elders - {first_choice["id"]})
    assert not (vb / "clips" / f"{first_choice['id']}.wav").exists()   # deleted


def test_probe_all_reaches_a_bucket_the_provisional_pass_zeroed(bank):
    """Without it, a crowded bucket is never probed — even once the thin ones
    have run out of clean voices, so the remaining slots go unfilled."""
    cv, vb, _ = bank
    kwargs = {"cv_root": cv, "max_speakers": 2, "accents": {"us"},
              "exclude_ids": {clip_id_for("aaaaaaaaaaaa01")},
              "fill_thin_from": {MY: 100, FE: 0}, "progress": lambda _m: None}
    capped = seed_from_common_voice(vb, **kwargs)
    assert {c.id for c in capped.clips} == {clip_id_for("bbbbbbbbbbbb02")}
    for c in capped.clips:
        (vb / c.clip_path).unlink()
    everything = seed_from_common_voice(vb, probe_all=True, **kwargs)
    assert {c.id for c in everything.clips} == {
        clip_id_for("bbbbbbbbbbbb02"), clip_id_for("dddddddddddd04")}


def test_a_bucket_that_runs_dry_after_build_failures_hands_its_slot_on(bank, monkeypatch):
    """Quotas are not fixed at probe time: when the thinnest bucket's only
    clean voice fails once built, the slot goes to the next-thinnest bucket
    instead of being lost. (--probe-all, so the crowded bucket has a probed
    candidate to give the slot to.)"""
    import lnvox.voices.common_voice as cvmod
    cv, vb, _ = bank
    real_probe = cvmod.probe_recording_quality
    elder = clip_id_for("bbbbbbbbbbbb02")

    def probe(path):
        m = real_probe(path)
        return {**m, "bandwidth": 4000.0} if Path(path).stem == elder else m

    monkeypatch.setattr(cvmod, "probe_recording_quality", probe)
    out = _grow(cv, vb, 1, probe_all=True)
    assert [c.id for c in out.clips] == [clip_id_for("dddddddddddd04")]


def test_a_rerun_reuses_cached_probes_and_still_rechecks_built_clips(bank, monkeypatch):
    import lnvox.voices.common_voice as cvmod
    cv, vb, _ = bank
    first = _grow(cv, vb, 10)
    assert (vb / "cv_probes.json").exists()
    for c in first.clips:
        (vb / c.clip_path).unlink()

    probed: list[Path] = []
    real_probe = cvmod.probe_recording_quality
    monkeypatch.setattr(cvmod, "probe_recording_quality",
                        lambda p: probed.append(Path(p)) or real_probe(p))
    second = _grow(cv, vb, 10)
    assert [c.id for c in second.clips] == [c.id for c in first.clips]
    assert all(p.parent == vb / "clips" for p in probed)        # no corpus re-probe
    assert len(probed) == len(second.clips)                     # each built clip re-measured


def test_a_cache_from_another_probe_version_is_ignored(bank):
    import json
    cv, vb, _ = bank
    muffled = {"bandwidth": 3000.0, "snr": 10.0, "rumble": 0, "hiss": 0, "clip": 0}
    (vb / "cv_probes.json").write_text(json.dumps(
        {"version": 0, "probes": {f"bbbbbbbbbbbb02_{k}.wav": muffled for k in range(3)}}))
    assert [c.id for c in _grow(cv, vb, 1).clips] == [clip_id_for("bbbbbbbbbbbb02")]
