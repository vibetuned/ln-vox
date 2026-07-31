"""Cross-volume voice continuity: PriorCastIndex + cast_book reuse.

All character names below are invented — no real book content.
"""

from lnvox.llm.schemas import Character
from lnvox.voices.matcher import PriorCastIndex, cast_book
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
    """LLM stand-in: fixed target inference + fixed top-ranked clip."""

    def __init__(self, target: VoiceTarget, pick: str):
        self.target = target
        self.pick = pick
        self.calls: list[str] = []

    def render(self, template, **kwargs):
        self.calls.append(template)
        return template

    def structured(self, *, system, user, schema, max_tokens):
        if schema is VoiceTarget:
            return self.target
        return MatchResult(
            ranked_choices=[_RankedChoice(clip_id=self.pick, score=0.9, reason="stub")]
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
