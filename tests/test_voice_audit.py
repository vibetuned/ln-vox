"""Casting audit: collision detection and repair (DESIGN.md §20).

All character names below are invented — no real book content.
"""

import json
from collections import Counter

from lnvox.voices.acoustics import semitones_apart
from lnvox.voices.audit import (
    find_collisions,
    repair_collisions,
    scene_cooccurrence,
)
from lnvox.voices.schema import (
    BookCasting, CharacterCasting, VoiceClip, Voicebank, VoiceTarget,
)


def _clip(cid, pitch, gender="male", age="adult"):
    return VoiceClip(id=cid, source="common_voice", clip_path=f"clips/{cid}.wav",
                     duration_seconds=12.0, gender=gender, age_band=age)


def _cast(name, cid, locked=False):
    return CharacterCasting(
        character_name=name,
        target=VoiceTarget(gender="male", age_band="adult"),
        assigned_clip_id=cid, locked=locked,
    )


# Pitches chosen so A/B are ~0.7 st apart and the spares are far from both.
ACOUSTICS = {
    "c_a": {"pitch": 100.0, "bandwidth": 11000, "snr": 50, "hiss": 0.1},
    "c_b": {"pitch": 104.0, "bandwidth": 11000, "snr": 50, "hiss": 0.1},
    "c_far": {"pitch": 160.0, "bandwidth": 11000, "snr": 50, "hiss": 0.1},
    "c_spare": {"pitch": 155.0, "bandwidth": 11000, "snr": 50, "hiss": 0.1},
    "c_muffled": {"pitch": 170.0, "bandwidth": 5000, "snr": 50, "hiss": 0.1},
}
BANK = Voicebank(clips=[
    _clip("c_a", 100.0), _clip("c_b", 104.0), _clip("c_far", 160.0),
    _clip("c_spare", 155.0), _clip("c_muffled", 170.0),
])


def test_semitones_apart():
    assert semitones_apart(100, 200) == 12.0
    assert semitones_apart(100, 100) == 0.0
    assert semitones_apart(0, 100) == 0.0   # unknown pitch never fabricates a gap


def _write_scenes(tmp_path, scenes):
    d = tmp_path / "02_scenes"
    d.mkdir()
    (d / "01.json").write_text(json.dumps({"chapter_id": "01", "scenes": scenes}))
    return d


def test_cooccurrence_weight_is_the_lesser_speaker(tmp_path):
    d = _write_scenes(tmp_path, [{"scene_id": "s1", "beats": [
        {"speaker": "Ada"}, {"speaker": "Ada"}, {"speaker": "Ada"},
        {"speaker": "Bram"},
    ]}])
    beats, pairs = scene_cooccurrence(d)
    assert beats["Ada"] == 3 and beats["Bram"] == 1
    assert pairs[("Ada", "Bram")] == 1


def test_cooccurrence_skips_speakerless_beats_and_sums_scenes(tmp_path):
    d = _write_scenes(tmp_path, [
        {"scene_id": "s1", "beats": [{"speaker": None}, {"speaker": "Ada"},
                                     {"speaker": "Bram"}]},
        {"scene_id": "s2", "beats": [{"speaker": "Ada"}, {"speaker": "Bram"}]},
    ])
    beats, pairs = scene_cooccurrence(d)
    assert None not in beats
    assert pairs[("Ada", "Bram")] == 2


def test_cooccurrence_counts_02_scenes_narration_as_the_narrator(tmp_path):
    """First launcher run: Stage A reads 02_scenes/, where narration beats
    have no speaker. They are the Narrator's (s3 assigns them so), and the
    Narrator must pair with the characters it shares scenes with."""
    d = _write_scenes(tmp_path, [{"scene_id": "s1", "beats": [
        {"type": "narration", "speaker": None},
        {"type": "dialogue", "speaker": "Ada"},
        {"type": "narration", "speaker": None},
        {"type": "dialogue", "speaker": "Ada"},
        {"type": "narration", "speaker": None},
        {"type": "dialogue", "speaker": None},          # unattributed line
    ]}])
    beats, pairs = scene_cooccurrence(d)
    assert beats["Narrator"] == 3 and beats["Ada"] == 2
    assert pairs[("Ada", "Narrator")] == 2
    assert None not in beats


def test_narrator_collision_is_found_from_02_scenes(tmp_path):
    d = _write_scenes(tmp_path, [{"scene_id": "s1", "beats": [
        {"type": "narration", "speaker": None}, {"type": "dialogue", "speaker": "Ada"},
    ] * 4}])
    _, pairs = scene_cooccurrence(d)
    casting = BookCasting(book_id="b", castings=[
        _cast("Narrator", "c_a"), _cast("Ada", "c_b")])
    cols = find_collisions(casting, ACOUSTICS, pairs)
    assert [(c.a, c.b) for c in cols] == [("Ada", "Narrator")]


def test_characters_in_different_scenes_never_pair(tmp_path):
    d = _write_scenes(tmp_path, [
        {"scene_id": "s1", "beats": [{"speaker": "Ada"}]},
        {"scene_id": "s2", "beats": [{"speaker": "Bram"}]},
    ])
    _, pairs = scene_cooccurrence(d)
    assert pairs == Counter()


def test_same_clip_is_a_collision():
    casting = BookCasting(book_id="b", castings=[_cast("Ada", "c_a"), _cast("Bram", "c_a")])
    cols = find_collisions(casting, ACOUSTICS, Counter({("Ada", "Bram"): 5}))
    assert len(cols) == 1 and cols[0].same_clip and cols[0].semitones == 0.0


def test_near_pitch_is_a_collision():
    casting = BookCasting(book_id="b", castings=[_cast("Ada", "c_a"), _cast("Bram", "c_b")])
    cols = find_collisions(casting, ACOUSTICS, Counter({("Ada", "Bram"): 5}))
    assert len(cols) == 1 and not cols[0].same_clip
    assert cols[0].semitones < 1.0


def test_separated_voices_are_not_flagged():
    casting = BookCasting(book_id="b", castings=[_cast("Ada", "c_a"), _cast("Bram", "c_far")])
    assert find_collisions(casting, ACOUSTICS, Counter({("Ada", "Bram"): 5})) == []


def test_pairs_below_min_shared_are_ignored():
    """In a 46-character book almost everyone co-occurs with someone once."""
    casting = BookCasting(book_id="b", castings=[_cast("Ada", "c_a"), _cast("Bram", "c_a")])
    assert find_collisions(casting, ACOUSTICS, Counter({("Ada", "Bram"): 1})) == []


def test_unassigned_character_is_skipped():
    casting = BookCasting(book_id="b", castings=[_cast("Ada", "c_a"), _cast("Bram", "")])
    assert find_collisions(casting, ACOUSTICS, Counter({("Ada", "Bram"): 5})) == []


def test_repair_moves_the_lower_speaking_character():
    casting = BookCasting(book_id="b", castings=[_cast("Ada", "c_a"), _cast("Bram", "c_b")])
    pairs = Counter({("Ada", "Bram"): 5})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    applied, unfixed = repair_collisions(
        casting, BANK, ACOUSTICS, Counter({"Ada": 50, "Bram": 4}), pairs, cols)
    assert unfixed == []
    assert [r.character for r in applied] == ["Bram"]
    by = {c.character_name: c.assigned_clip_id for c in casting.castings}
    assert by["Ada"] == "c_a"                       # dominant voice untouched
    assert by["Bram"] not in ("c_a", "c_b")


def test_repair_never_moves_the_narrator():
    casting = BookCasting(book_id="b", castings=[
        _cast("Narrator", "c_a"), _cast("Bram", "c_b")])
    pairs = Counter({("Bram", "Narrator"): 5})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    # Narrator speaks far more, but the guard must hold even when it speaks less.
    applied, _ = repair_collisions(
        casting, BANK, ACOUSTICS, Counter({"Narrator": 1, "Bram": 99}), pairs, cols)
    assert [r.character for r in applied] == ["Bram"]
    assert {c.character_name: c.assigned_clip_id
            for c in casting.castings}["Narrator"] == "c_a"


def test_repair_never_moves_a_locked_casting():
    casting = BookCasting(book_id="b", castings=[
        _cast("Ada", "c_a"), _cast("Bram", "c_a", locked=True)])
    pairs = Counter({("Ada", "Bram"): 5})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    applied, unfixed = repair_collisions(
        casting, BANK, ACOUSTICS, Counter({"Ada": 50, "Bram": 4}), pairs, cols)
    # Bram speaks less but is locked, so Ada moves instead.
    assert [r.character for r in applied] == ["Ada"]
    assert {c.character_name: c.assigned_clip_id
            for c in casting.castings}["Bram"] == "c_a"


def test_both_sides_protected_reports_unfixed():
    casting = BookCasting(book_id="b", castings=[
        _cast("Narrator", "c_a"), _cast("Bram", "c_a", locked=True)])
    pairs = Counter({("Bram", "Narrator"): 5})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    applied, unfixed = repair_collisions(
        casting, BANK, ACOUSTICS, Counter({"Narrator": 9, "Bram": 4}), pairs, cols)
    assert applied == [] and len(unfixed) == 1


def test_replacement_must_clear_every_scene_partner():
    """c_spare is far from Ada but close to Cyra, who also shares Bram's scenes."""
    casting = BookCasting(book_id="b", castings=[
        _cast("Ada", "c_a"), _cast("Bram", "c_b"), _cast("Cyra", "c_far")])
    pairs = Counter({("Ada", "Bram"): 5, ("Bram", "Cyra"): 4})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    applied, unfixed = repair_collisions(
        casting, BANK, ACOUSTICS, Counter({"Ada": 50, "Bram": 4, "Cyra": 20}), pairs, cols)
    # Only c_muffled clears both, and it's below the quality floor → unfixable.
    assert applied == [] and len(unfixed) == 1


def test_repair_empties_a_descriptor_written_for_the_old_clip():
    """s3 uses a stored voice_descriptor verbatim, so one describing the clip
    the audit moved away from would contradict the new voice."""
    bram = _cast("Bram", "c_b").model_copy(update={"voice_descriptor": "for c_b"})
    casting = BookCasting(book_id="b", castings=[_cast("Ada", "c_a"), bram])
    pairs = Counter({("Ada", "Bram"): 5})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    repair_collisions(casting, BANK, ACOUSTICS,
                      Counter({"Ada": 50, "Bram": 4}), pairs, cols)
    moved = {c.character_name: c for c in casting.castings}["Bram"]
    assert moved.assigned_clip_id != "c_b" and moved.voice_descriptor == ""


def test_repair_records_provenance():
    casting = BookCasting(book_id="b", castings=[_cast("Ada", "c_a"), _cast("Bram", "c_b")])
    pairs = Counter({("Ada", "Bram"): 5})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    repair_collisions(casting, BANK, ACOUSTICS,
                      Counter({"Ada": 50, "Bram": 4}), pairs, cols)
    note = {c.character_name: c.audit_note for c in casting.castings}["Bram"]
    assert "Ada" in note and "semitones" in note


def test_repair_never_moves_the_same_character_twice():
    """Oz put Scarecrow, Tin Woodman, Cowardly Lion and Oz on ONE clip. Each
    pair is a separate collision, so without a guard the Lion gets moved once
    per pair and can land back where it started."""
    casting = BookCasting(book_id="b", castings=[
        _cast("Scarecrow", "c_a"), _cast("Woodman", "c_a"), _cast("Lion", "c_a")])
    pairs = Counter({("Scarecrow", "Woodman"): 40,
                     ("Lion", "Scarecrow"): 30,
                     ("Lion", "Woodman"): 25})
    beats = Counter({"Scarecrow": 121, "Woodman": 83, "Lion": 79})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    applied, _ = repair_collisions(casting, BANK, ACOUSTICS, beats, pairs, cols)
    assert len(applied) == len({r.character for r in applied})


def test_repair_skips_pairs_already_resolved_by_an_earlier_fix():
    """Moving Woodman off the shared clip also resolves Lion+Woodman, which
    must not then be reported unfixed."""
    # Local bank: needs a spare that clears BOTH 100 Hz and 160 Hz, which the
    # shared fixture deliberately lacks (see the every-partner test below).
    acoustics = dict(ACOUSTICS)
    acoustics["c_mid"] = {"pitch": 128.0, "bandwidth": 11000, "snr": 50, "hiss": 0.1}
    bank = Voicebank(clips=list(BANK.clips) + [_clip("c_mid", 128.0)])

    casting = BookCasting(book_id="b", castings=[
        _cast("Scarecrow", "c_a"), _cast("Woodman", "c_a"), _cast("Lion", "c_far")])
    pairs = Counter({("Scarecrow", "Woodman"): 40, ("Lion", "Woodman"): 25})
    beats = Counter({"Scarecrow": 121, "Woodman": 83, "Lion": 79})
    cols = find_collisions(casting, acoustics, pairs)
    assert len(cols) == 1          # only Scarecrow+Woodman start colliding
    applied, unfixed = repair_collisions(casting, bank, acoustics, beats, pairs, cols)
    assert [r.character for r in applied] == ["Woodman"]
    assert unfixed == []
    assert find_collisions(casting, acoustics, pairs) == []


def test_every_repaired_pair_ends_separated():
    casting = BookCasting(book_id="b", castings=[
        _cast("Scarecrow", "c_a"), _cast("Woodman", "c_a")])
    pairs = Counter({("Scarecrow", "Woodman"): 40})
    beats = Counter({"Scarecrow": 121, "Woodman": 83})
    cols = find_collisions(casting, ACOUSTICS, pairs)
    repair_collisions(casting, BANK, ACOUSTICS, beats, pairs, cols)
    assert find_collisions(casting, ACOUSTICS, pairs) == []


def test_a_character_may_move_twice_but_never_returns_to_a_previous_clip():
    """Late collisions can involve two characters that both already moved.
    Blocking a second move stalls the repair; allowing a RETURN cycles."""
    acoustics = dict(ACOUSTICS)
    for cid, hz in (("c_x", 200.0), ("c_y", 250.0), ("c_z", 300.0)):
        acoustics[cid] = {"pitch": hz, "bandwidth": 11000, "snr": 50, "hiss": 0.1}
    bank = Voicebank(clips=list(BANK.clips) + [
        _clip("c_x", 200.0), _clip("c_y", 250.0), _clip("c_z", 300.0)])

    casting = BookCasting(book_id="b", castings=[
        _cast("Ada", "c_a"), _cast("Bram", "c_a"), _cast("Cyra", "c_b")])
    pairs = Counter({("Ada", "Bram"): 40, ("Bram", "Cyra"): 30})
    beats = Counter({"Ada": 100, "Bram": 60, "Cyra": 50})
    cols = find_collisions(casting, acoustics, pairs)
    repair_collisions(casting, bank, acoustics, beats, pairs, cols)

    final = {c.character_name: c.assigned_clip_id for c in casting.castings}
    assert len(set(final.values())) == 3          # everyone distinct
    assert find_collisions(casting, acoustics, pairs) == []


def _run_all():
    """Standalone runner (no pytest): hands `tmp_path` tests a fresh dir."""
    import inspect
    import tempfile
    from pathlib import Path

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        if "tmp_path" in inspect.signature(fn).parameters:
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp))
        else:
            fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()
