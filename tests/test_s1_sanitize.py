"""s1 cast sanitation: prompt-example leakage, pronoun names, narrator naming.

All character names and prose below are invented — no real book content.
"""

from lnvox.llm.schemas import Character, CharacterList
from lnvox.stages.s1_characters import (
    NARRATOR_NAME,
    _attested_in,
    sanitize_character_list,
)

TEXT = (
    "The lantern swung over the gate. Corvin Ashe set down his pack and "
    "waited. A pattern of frost had crept across the stones. The Gatekeeper "
    "watched him from the arch, saying nothing, and the Four Sons of the "
    "house stood behind her."
)


def _c(name, aliases=(), gender="male"):
    return Character(
        name=name, aliases=list(aliases), gender=gender,
        approx_age="adult", description="d", evidence=[],
    )


def _names(cl):
    return [c.name for c in cl.characters]


def test_drops_name_absent_from_the_chapter():
    """The extractor copying its own few-shot example into the output."""
    cl = CharacterList(characters=[_c("Corvin Ashe"), _c("Sir Patrick", ["Pat"])])
    assert _names(sanitize_character_list(cl, TEXT)) == ["Corvin Ashe"]


def test_short_alias_must_match_on_a_word_boundary():
    """'Pat' occurs inside 'pattern'; a bare substring test keeps the ghost."""
    assert not _attested_in(TEXT.lower(), "Sir Patrick", ["Pat"])


def test_keeps_descriptive_name_whose_tokens_all_appear():
    cl = CharacterList(characters=[_c("The Four Sons"), _c("The Gatekeeper")])
    assert _names(sanitize_character_list(cl, TEXT)) == [
        "The Four Sons", "The Gatekeeper",
    ]


def test_keeps_character_named_only_by_an_alias():
    cl = CharacterList(characters=[_c("The Wanderer", ["Corvin Ashe"])])
    assert _names(sanitize_character_list(cl, TEXT)) == ["The Wanderer"]


def test_drops_pronoun_named_characters():
    cl = CharacterList(characters=[
        _c("I", gender="male"), _c("I", gender="female"),
        _c("She"), _c("Corvin Ashe"),
    ])
    assert _names(sanitize_character_list(cl, TEXT)) == ["Corvin Ashe"]


def test_narrator_is_canonicalised_for_the_voice_matcher():
    """The matcher special-cases `name == "Narrator"` exactly; "The Narrator"
    would otherwise be cast as an unrelated extra."""
    cl = CharacterList(characters=[_c("The Narrator"), _c("Corvin Ashe")])
    assert _names(sanitize_character_list(cl, TEXT)) == [
        NARRATOR_NAME, "Corvin Ashe",
    ]


def test_narrator_survives_attestation():
    """A first-person book never says "the narrator" — without an exemption the
    attestation check drops the protagonist."""
    cl = CharacterList(characters=[_c("The Narrator")])
    assert _names(sanitize_character_list(cl, "Nothing relevant here.")) == [
        NARRATOR_NAME
    ]


def test_narrator_variants_collapse_to_one_entry():
    cl = CharacterList(characters=[_c("The Narrator"), _c("Narrator")])
    assert _names(sanitize_character_list(cl, TEXT)) == [NARRATOR_NAME]


def test_empty_list_is_preserved():
    assert sanitize_character_list(CharacterList(characters=[]), TEXT).characters == []


def test_clean_cast_is_untouched():
    cl = CharacterList(characters=[_c("Corvin Ashe"), _c("The Gatekeeper")])
    assert _names(sanitize_character_list(cl, TEXT)) == [
        "Corvin Ashe", "The Gatekeeper",
    ]
