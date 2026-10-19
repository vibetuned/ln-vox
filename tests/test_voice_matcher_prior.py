"""Cross-volume voice continuity: PriorCastIndex + cast_book reuse.

All character names below are invented — no real book content.
"""

from lnvox.llm.schemas import Character
import json

from lnvox.voices.matcher import PriorCastIndex, _vary, cast_book, shared_name_words
from lnvox.voices.schema import (
    BookCasting,
    CharacterCasting,
    MatchResult,
    VoiceClip,
    Voicebank,
    VoiceTarget,
    _RankedChoice,
)


def _char(name, aliases=(), gender="male", age="teen"):
    return Character(
        name=name,
        aliases=list(aliases),
        gender=gender,
        approx_age=age,
        description="test character",
        evidence=[],
    )


def _casting(name, clip_id, gender="male", age="teen"):
    return CharacterCasting(
        character_name=name,
        target=VoiceTarget(gender=gender, age_band=age),
        candidates_considered=1,
        ranked=[_RankedChoice(clip_id=clip_id, score=1.0, reason="test")] if clip_id else [],
        assigned_clip_id=clip_id,
    )


def _volume(book_id, castings):
    return BookCasting(book_id=book_id, castings=castings)


def _clip(clip_id, gender="male", age="teen"):
    return VoiceClip(
        id=clip_id,
        source="manual",
        clip_path=f"{clip_id}.wav",
        duration_seconds=12.0,
        gender=gender,
        age_band=age,
        accent="us",
    )


class StubClient:
    """LLM stand-in: fixed target inference + fixed ranking (one id or a
    list, in rank order)."""

    def __init__(self, target: VoiceTarget, pick):
        self.target = target
        self.picks = [pick] if isinstance(pick, str) else list(pick)
        self.calls: list[str] = []

    def render(self, template, **kwargs):
        self.calls.append(template)
        return template

    def structured(self, *, system, user, schema, max_tokens):
        if schema is VoiceTarget:
            return self.target
        return MatchResult(
            ranked_choices=[
                _RankedChoice(clip_id=p, score=0.9, reason="stub") for p in self.picks
            ]
        )


def test_lookup_bridges_canonical_rename_via_alias():
    idx = PriorCastIndex()
    idx.add_volume(
        _volume("s/volume-01", [_casting("Aldric Vane", "m1")]),
        [_char("Aldric Vane", aliases=["Aldric"])],
    )
    # Next volume s1 shortened the canonical name; alias carries the old one.
    hit = idx.lookup("Aldric", aliases=["Aldric Vane"], gender="male")
    assert hit is not None and hit.assigned_clip_id == "m1"
    # Bridging works from the prior volume's aliases alone too.
    hit = idx.lookup("Aldric", gender="male")
    assert hit is not None and hit.assigned_clip_id == "m1"


def test_lookup_matches_flipped_name_order():
    idx = PriorCastIndex()
    idx.add_volume(
        _volume("s/volume-01", [_casting("Sera Kalt", "f1", gender="female")]),
        [_char("Sera Kalt", gender="female")],
    )
    hit = idx.lookup("Kalt Sera", gender="female")
    assert hit is not None and hit.assigned_clip_id == "f1"


def test_newest_volume_wins_and_gaps_survive():
    idx = PriorCastIndex()
    idx.add_volume(_volume("s/volume-01", [_casting("Aldric", "m1")]), [_char("Aldric")])
    idx.add_volume(_volume("s/volume-02", [_casting("Brann", "m9")]), [_char("Brann")])
    idx.add_volume(_volume("s/volume-03", [_casting("Aldric", "m2")]), [_char("Aldric")])
    # Aldric skipped volume-02 entirely; newest assignment still found.
    assert idx.lookup("Aldric").assigned_clip_id == "m2"
    assert idx.lookup("Brann").assigned_clip_id == "m9"


def test_ambiguous_alias_key_is_dropped_but_names_survive():
    idx = PriorCastIndex()
    idx.add_volume(
        _volume(
            "s/volume-01",
            [_casting("Aldric", "m1"), _casting("Brann", "m2")],
        ),
        [
            _char("Aldric", aliases=["the Captain"]),
            _char("Brann", aliases=["the Captain"]),
        ],
    )
    assert idx.lookup("the Captain") is None
    assert idx.lookup("Aldric").assigned_clip_id == "m1"
    assert idx.lookup("Brann").assigned_clip_id == "m2"


def test_unassigned_entry_does_not_shadow_older_assignment():
    idx = PriorCastIndex()
    idx.add_volume(_volume("s/volume-01", [_casting("Aldric", "m1")]), [_char("Aldric")])
    idx.add_volume(_volume("s/volume-02", [_casting("Aldric", "")]), [_char("Aldric")])
    assert idx.lookup("Aldric").assigned_clip_id == "m1"


def test_alias_match_requires_gender_agreement():
    idx = PriorCastIndex()
    idx.add_volume(
        _volume("s/volume-01", [_casting("Aldric", "m1", gender="male")]),
        [_char("Aldric", aliases=["Vane"])],
    )
    # Same alias, different-gender character: no fuse via alias...
    assert idx.lookup("Sera", aliases=["Vane"], gender="female") is None
    # ...but a canonical name matching on both sides is trusted as-is.
    assert idx.lookup("Aldric", gender="female") is not None


def test_companion_alias_cannot_steal_a_name_key():
    # s1 sometimes lists a companion's name among a character's aliases.
    # A newer volume where "Sera" carries the bogus alias "Aldric" must not
    # hijack lookups for the real Aldric, even though his own volume is older.
    idx = PriorCastIndex()
    idx.add_volume(
        _volume("s/volume-01", [_casting("Aldric", "m1", gender="male")]),
        [_char("Aldric", aliases=["Aldric Vane"])],
    )
    idx.add_volume(
        _volume("s/volume-02", [_casting("Sera", "f1", gender="female")]),
        [_char("Sera", aliases=["Aldric"], gender="female")],
    )
    # Current Aldric with known gender: alias-mediated female hit is rejected,
    # the male entry is found through his other keys.
    hit = idx.lookup("Aldric", aliases=["Aldric Vane"], gender="male")
    assert hit is not None and hit.assigned_clip_id == "m1"
    # With unknown gender, an alias-mediated match is never trusted: the
    # stolen key resolves to nothing rather than to the wrong voice.
    assert idx.lookup("Aldric", gender="unknown") is None


def test_a_shared_family_name_cannot_carry_a_voice_to_a_namesake():
    # s1 lists a bare family name among a character's aliases ("Vane" for
    # Aldric Vane). A later volume's "Vane" — another character, cast next to
    # Aldric Vane — must not inherit his voice through it: the gender check
    # can't tell two men apart.
    idx = PriorCastIndex()
    idx.add_volume(
        _volume("s/volume-01", [_casting("Aldric", "m1")]),
        [_char("Aldric", aliases=["Vane"])],
    )
    cast = [_char("Aldric", aliases=["Aldric Vane"]), _char("Vane", aliases=["Vane Oro"])]
    shared = shared_name_words(cast)
    assert shared["Vane"] == {"vane"}
    assert idx.lookup("Vane", ["Vane Oro"], "male", shared["Vane"]) is None
    hit = idx.lookup("Aldric", ["Aldric Vane"], "male", shared["Aldric"])
    assert hit is not None and hit.assigned_clip_id == "m1"
    # With no namesake in the cast the bare-name alias still bridges.
    assert idx.lookup("Vane", gender="male").assigned_clip_id == "m1"


def test_cast_book_casts_a_namesake_fresh():
    vb = Voicebank(clips=[
        _clip("m1"), _clip("m2"), _clip("f_nar", gender="female", age="adult"),
    ])
    idx = PriorCastIndex()
    idx.add_volume(
        _volume("s/volume-01", [
            _casting("Aldric", "m1"),
            _casting("Narrator", "f_nar", gender="female", age="adult"),
        ]),
        [_char("Aldric", aliases=["Vane"])],
    )
    client = StubClient(VoiceTarget(gender="male", age_band="teen"), "m2")
    result = cast_book(
        client,
        "s/volume-02",
        [_char("Aldric", aliases=["Aldric Vane"]), _char("Vane", aliases=["Vane Oro"])],
        vb,
        prior_index=idx,
    )
    by_name = {c.character_name: c.assigned_clip_id for c in result.castings}
    assert by_name == {"Aldric": "m1", "Vane": "m2", "Narrator": "f_nar"}


def test_cast_book_reuses_prior_without_llm():
    vb = Voicebank(clips=[_clip("m1"), _clip("f_nar", gender="female", age="adult")])
    idx = PriorCastIndex()
    idx.add_volume(
        _volume(
            "s/volume-01",
            [
                _casting("Aldric Vane", "m1"),
                _casting("Narrator", "f_nar", gender="female", age="adult"),
            ],
        ),
        [_char("Aldric Vane")],
    )
    client = StubClient(VoiceTarget(gender="male", age_band="teen"), "m1")
    result = cast_book(
        client,
        "s/volume-02",
        [_char("Aldric", aliases=["Aldric Vane"])],
        vb,
        prior_index=idx,
    )
    by_name = {c.character_name: c for c in result.castings}
    assert by_name["Aldric"].assigned_clip_id == "m1"
    assert by_name["Narrator"].assigned_clip_id == "f_nar"
    assert client.calls == []  # pure reuse: the LLM was never consulted


def test_cast_book_recasts_internally_contradictory_prior():
    # Prior assignment points a female-target character at a male clip
    # (a hand-edit gone wrong): must recast, not propagate.
    vb = Voicebank(
        clips=[_clip("m_wrong", gender="male", age="young_adult"),
               _clip("f_good", gender="female", age="adult")]
    )
    idx = PriorCastIndex()
    idx.add_volume(
        _volume(
            "s/volume-01",
            [_casting("Miss Hollis", "m_wrong", gender="female", age="adult")],
        ),
        [_char("Miss Hollis", gender="female", age="adult")],
    )
    client = StubClient(VoiceTarget(gender="female", age_band="adult"), "f_good")
    result = cast_book(
        client,
        "s/volume-02",
        [_char("Miss Hollis", gender="female", age="adult")],
        vb,
        prior_index=idx,
    )
    by_name = {c.character_name: c for c in result.castings}
    assert by_name["Miss Hollis"].assigned_clip_id == "f_good"
    assert client.calls  # a fresh LLM cast actually happened


# --- This volume's own casting (`existing`): locks survive a re-cast --------


def _locked(name, clip_id, note="set by hand", gender="male", age="teen", descriptor=""):
    c = _casting(name, clip_id, gender=gender, age=age)
    return c.model_copy(
        update={"locked": True, "audit_note": note, "voice_descriptor": descriptor}
    )


def _series_bank():
    return Voicebank(clips=[
        _clip("m1"), _clip("m2"), _clip("m3"),
        _clip("f_nar", gender="female", age="adult"),
        _clip("f_alt", gender="female", age="adult"),
    ])


def _vol1_index():
    idx = PriorCastIndex()
    idx.add_volume(
        _volume("s/volume-01", [
            _casting("Aldric", "m1"),
            _casting("Narrator", "f_nar", gender="female", age="adult"),
        ]),
        [_char("Aldric")],
    )
    return idx


def test_locked_own_entry_beats_prior_volume_and_keeps_its_fields():
    """A hand re-cast of a recurring character used to be undone by the next
    `voice cast`: the prior volume won, and locked/audit_note were dropped."""
    existing = _volume("s/volume-02", [_locked("Aldric", "m2", note="deeper voice")])
    client = StubClient(VoiceTarget(gender="male", age_band="teen"), "m3")
    result = cast_book(
        client, "s/volume-02", [_char("Aldric")], _series_bank(),
        prior_index=_vol1_index(), existing=existing,
    )
    aldric = {c.character_name: c for c in result.castings}["Aldric"]
    assert aldric.assigned_clip_id == "m2"          # not vol-1's m1
    assert aldric.locked and aldric.audit_note == "deeper voice"
    assert client.calls == []


def test_unlocked_own_entry_loses_to_prior_but_keeps_first_appearances():
    existing = _volume("s/volume-02", [
        _casting("Aldric", "m2").model_copy(update={"audit_note": "old note"}),
        _casting("Brann", "m3").model_copy(update={"audit_note": "voice audit: moved"}),
    ])
    client = StubClient(VoiceTarget(gender="male", age_band="teen"), "m1")
    result = cast_book(
        client, "s/volume-02", [_char("Aldric"), _char("Brann")], _series_bank(),
        prior_index=_vol1_index(), existing=existing,
    )
    by = {c.character_name: c for c in result.castings}
    # Continuity chain wins over an unlocked own entry...
    assert by["Aldric"].assigned_clip_id == "m1"
    assert by["Aldric"].audit_note == ""            # note was about m2
    # ...but a first-appearance character keeps its clip and provenance.
    assert by["Brann"].assigned_clip_id == "m3"
    assert by["Brann"].audit_note == "voice audit: moved"
    assert not by["Brann"].locked
    assert client.calls == []


def test_voice_descriptor_carries_over_only_while_the_clip_is_unchanged():
    existing = _volume("s/volume-02", [
        # Written for m2, but vol-1 puts Aldric back on m1: must not follow.
        _casting("Aldric", "m2").model_copy(update={"voice_descriptor": "for m2"}),
        _casting("Brann", "m3").model_copy(update={"voice_descriptor": "for m3"}),
    ])
    result = cast_book(
        StubClient(VoiceTarget(gender="male", age_band="teen"), "m1"),
        "s/volume-02", [_char("Aldric"), _char("Brann")], _series_bank(),
        prior_index=_vol1_index(), existing=existing,
    )
    by = {c.character_name: c for c in result.castings}
    assert by["Aldric"].voice_descriptor == ""
    assert by["Brann"].voice_descriptor == "for m3"


def test_locked_narrator_beats_narrator_clip_override():
    existing = _volume("s/volume-02", [
        _locked("Narrator", "f_alt", gender="female", age="adult"),
    ])
    result = cast_book(
        StubClient(VoiceTarget(gender="male", age_band="teen"), "m1"),
        "s/volume-02", [], _series_bank(),
        prior_index=_vol1_index(), existing=existing,
        narrator_clip_override="f_nar",
    )
    narrator = result.castings[-1]
    assert narrator.character_name == "Narrator"
    assert narrator.assigned_clip_id == "f_alt" and narrator.locked
    # Set Voice empties the descriptor; the Narrator's is rebuilt from the clip.
    assert "female" in narrator.voice_descriptor


def test_locked_entry_with_vanished_clip_is_recast_and_says_so():
    existing = _volume("s/volume-02", [_locked("Brann", "m_gone")])
    client = StubClient(VoiceTarget(gender="male", age_band="teen"), "m3")
    result = cast_book(
        client, "s/volume-02", [_char("Brann")], _series_bank(), existing=existing,
    )
    brann = {c.character_name: c for c in result.castings}["Brann"]
    assert brann.assigned_clip_id == "m3" and not brann.locked
    assert "m_gone" in brann.audit_note


def test_unknown_narrator_clip_raises_before_any_llm_call():
    client = StubClient(VoiceTarget(gender="male", age_band="teen"), "m1")
    try:
        cast_book(
            client, "s/volume-01", [_char("Aldric"), _char("Brann")], _series_bank(),
            narrator_clip_override="f_typo",
        )
    except ValueError as e:
        assert "f_typo" in str(e)
    else:
        raise AssertionError("expected ValueError")
    assert client.calls == []   # nobody was cast (or paid for) first


# --- Ranked clip ids are checked against the candidates offered -------------


def test_invented_clip_id_is_dropped_from_the_ranking():
    client = StubClient(
        VoiceTarget(gender="male", age_band="teen"), ["cv_invented", "m2", "f_nar"]
    )
    result = cast_book(client, "b", [_char("Aldric")], _series_bank())
    aldric = {c.character_name: c for c in result.castings}["Aldric"]
    # f_nar exists in the bank but was never offered (wrong gender/age).
    assert [r.clip_id for r in aldric.ranked] == ["m2"]
    assert aldric.assigned_clip_id == "m2"
    assert aldric.audit_note == ""


def test_no_offered_id_falls_back_to_first_candidate_and_records_it():
    client = StubClient(VoiceTarget(gender="male", age_band="teen"), ["cv_nope"])
    result = cast_book(client, "b", [_char("Aldric")], _series_bank())
    aldric = {c.character_name: c for c in result.castings}["Aldric"]
    first = _vary([c for c in _series_bank().clips if c.gender == "male"], "|b|Aldric", set())[0].id
    assert aldric.assigned_clip_id == first         # first in this character's order
    assert aldric.ranked[0].clip_id == first and aldric.ranked[0].score == 0.0
    assert "fell back" in aldric.audit_note


# --- Variety: a fresh cast spreads over the bank -------------------------------


class FirstOfferedClient(StubClient):
    """An LLM with pure position bias: ranks the candidates as offered."""

    def __init__(self, target: VoiceTarget):
        super().__init__(target, [])
        self.offered: list[list[str]] = []

    def render(self, template, **kwargs):
        if "candidates_json" in kwargs:
            self.offered.append([c["id"] for c in json.loads(kwargs["candidates_json"])])
        return super().render(template, **kwargs)

    def structured(self, *, system, user, schema, max_tokens):
        if schema is VoiceTarget:
            return self.target
        return MatchResult(
            ranked_choices=[_RankedChoice(clip_id=i, score=0.9, reason="first") for i in self.offered[-1][:3]]
        )


def _boys(n):
    return [_char(f"Squire {i}") for i in range(n)]


def test_fresh_casts_take_free_clips_first():
    bank = Voicebank(clips=[_clip(f"m{i}") for i in range(6)])
    client = FirstOfferedClient(VoiceTarget(gender="male", age_band="teen"))
    result = cast_book(client, "b", _boys(6), bank)
    clips = [c.assigned_clip_id for c in result.castings if c.character_name != "Narrator"]
    assert sorted(clips) == [f"m{i}" for i in range(6)]  # six characters, six voices
    # Each character saw the bank in an order of its own.
    assert len({tuple(o) for o in client.offered}) > 1


def test_an_llm_favourite_voices_one_character_only():
    # What casting a real book showed: three clips the ranker puts first
    # whenever they're offered. Without taking them off the list once used,
    # four characters ended up on one voice.
    favourites = ["m0", "m1", "m2"]

    class FavouriteClient(FirstOfferedClient):
        def structured(self, *, system, user, schema, max_tokens):
            if schema is VoiceTarget:
                return self.target
            offered = self.offered[-1]
            ranked = [f for f in favourites if f in offered] + [i for i in offered if i not in favourites]
            return MatchResult(ranked_choices=[_RankedChoice(clip_id=i, score=0.9, reason="fav") for i in ranked[:3]])

    bank = Voicebank(clips=[_clip(f"m{i}") for i in range(10)])
    result = cast_book(FavouriteClient(VoiceTarget(gender="male", age_band="teen")), "b", _boys(8), bank)
    clips = [c.assigned_clip_id for c in result.castings if c.character_name != "Narrator"]
    assert len(set(clips)) == 8


def test_a_clip_is_shared_only_when_the_bank_runs_out():
    bank = Voicebank(clips=[_clip("m1"), _clip("m2")])
    result = cast_book(FirstOfferedClient(VoiceTarget(gender="male", age_band="teen")), "b", _boys(3), bank)
    clips = [c.assigned_clip_id for c in result.castings if c.character_name != "Narrator"]
    assert sorted(clips[:2]) == ["m1", "m2"] and clips[2] in ("m1", "m2")


def test_a_spent_bank_shares_an_earlier_volumes_clip_not_this_ones():
    # A long series used up the band: m1 and m2 belong to earlier volumes,
    # m3 to a recurring character listed AFTER the newcomers. Neither
    # newcomer may take m3, nor the clip the other newcomer just took.
    bank = Voicebank(clips=[_clip("m1"), _clip("m2"), _clip("m3")])
    prior = PriorCastIndex()
    prior.add_volume(_volume("s/volume-01", [
        _casting("Old Knight", "m1"), _casting("Old Squire", "m2"), _casting("Brann", "m3"),
    ]))
    client = FirstOfferedClient(VoiceTarget(gender="male", age_band="teen"))
    result = cast_book(
        client, "s/volume-02", [_char("Newcomer A"), _char("Newcomer B"), _char("Brann")],
        bank, prior_index=prior, top_n=1,
    )
    clips = {c.character_name: c.assigned_clip_id for c in result.castings}
    assert clips["Brann"] == "m3"
    assert sorted([clips["Newcomer A"], clips["Newcomer B"]]) == ["m1", "m2"]
    assert all("m3" not in offered for offered in client.offered[:2])


def test_clips_of_earlier_volumes_and_locks_are_kept_free():
    bank = Voicebank(clips=[_clip("m1"), _clip("m2"), _clip("m3")])
    prior = PriorCastIndex()
    prior.add_volume(_volume("s/volume-01", [_casting("Old Knight", "m1")]))
    existing = _volume("s/volume-02", [_casting("Hand Picked", "m2").model_copy(update={"locked": True})])
    result = cast_book(
        FirstOfferedClient(VoiceTarget(gender="male", age_band="teen")), "s/volume-02",
        [_char("Newcomer")], bank, prior_index=prior, existing=existing,
    )
    assert {c.character_name: c.assigned_clip_id for c in result.castings}["Newcomer"] == "m3"


def test_the_seed_changes_the_draw_and_repeats_it():
    bank = Voicebank(clips=[_clip(f"m{i}") for i in range(8)])

    def first_pick(seed):
        client = FirstOfferedClient(VoiceTarget(gender="male", age_band="teen"))
        return cast_book(client, "b", [_char("Aldric")], bank, seed=seed).castings[0].assigned_clip_id

    assert first_pick("") == first_pick("")
    assert len({first_pick(str(s)) for s in range(6)}) > 1


# --- Narrator auto-cast relaxation ladder ------------------------------------


def _narrator_only(bank, picks=()):
    client = StubClient(VoiceTarget(gender="female", age_band="adult"), list(picks))
    return cast_book(client, "b", [], bank).castings[-1]


def test_narrator_ladder_prefers_england_but_never_filters_on_accent():
    us = _clip("f_us", gender="female", age="adult")          # accent "us"
    bank = Voicebank(clips=[us, _clip("m1")])
    n = _narrator_only(bank)
    assert n.assigned_clip_id == "f_us"
    assert n.ranked[0].reason == "Sole candidate (female/adult, england first)."


def test_narrator_ladder_relaxes_age_then_gender():
    n = _narrator_only(Voicebank(clips=[_clip("f_ya", gender="female", age="young_adult"),
                                        _clip("m1")]))
    assert n.assigned_clip_id == "f_ya" and "relaxed age" in n.ranked[0].reason
    n = _narrator_only(Voicebank(clips=[_clip("m1")]))
    assert n.assigned_clip_id == "m1" and "any clip" in n.ranked[0].reason


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()
