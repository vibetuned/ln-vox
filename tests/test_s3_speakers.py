"""s3 speaker canonicalisation (s2 tags → cast names).

All names and prose below are invented — no real book content.
"""

from lnvox.llm.schemas import (
    Beat, Character, CharacterList, ChapterScenes, Scene,
)
from lnvox.stages.s3_director import (
    build_speaker_index,
    canonical_speaker,
    canonicalize_scene_speakers,
)

CAST = CharacterList(characters=[
    Character(name="Narrator", aliases=[], gender="male",
              approx_age="adult", description="d", evidence=[]),
    Character(name="A SQUARE", aliases=["The Square"], gender="male",
              approx_age="adult", description="d", evidence=[]),
    Character(name="The Gatekeeper", aliases=["Corvin Ashe"], gender="female",
              approx_age="elder", description="d", evidence=[]),
])
IDX = build_speaker_index(CAST)


def test_exact_cast_name_is_unchanged():
    assert canonical_speaker("The Gatekeeper", IDX) == "The Gatekeeper"


def test_article_difference_resolves():
    """s2 writes 'The Square' for a cast entry named 'A SQUARE'."""
    assert canonical_speaker("The Square", IDX) == "A SQUARE"


def test_alias_resolves_to_canonical_name():
    assert canonical_speaker("Corvin Ashe", IDX) == "The Gatekeeper"


def test_the_narrator_resolves_to_the_matcher_s_exact_name():
    assert canonical_speaker("The Narrator", IDX) == "Narrator"
    assert canonical_speaker("narrator", IDX) == "Narrator"


def test_narration_speaker_none_passes_through():
    """s3 assigns narration the Narrator later; don't pre-empt it."""
    assert canonical_speaker(None, IDX) is None


def test_unknown_speaker_is_left_alone():
    """Better to keep the tag than to silently attach it to the wrong cast
    member; s3 renders it descriptor-only."""
    assert canonical_speaker("Some Passerby", IDX) == "Some Passerby"


def test_canonical_name_beats_an_alias_claiming_the_same_key():
    cast = CharacterList(characters=[
        Character(name="The Square", aliases=[], gender="male",
                  approx_age="adult", description="d", evidence=[]),
        Character(name="Other", aliases=["A Square"], gender="male",
                  approx_age="adult", description="d", evidence=[]),
    ])
    assert canonical_speaker("Square", build_speaker_index(cast)) == "The Square"


def _scenes(*speakers):
    return [ChapterScenes(chapter_id="01", scenes=[Scene(
        scene_id="01_s1", summary="s",
        beats=[Beat(type="dialogue" if sp else "narration", text="t",
                    speaker=sp, source_span="t") for sp in speakers],
    )])]


def test_canonicalize_rewrites_in_place_and_counts():
    cs = _scenes("The Square", "The Narrator", "The Gatekeeper", None)
    changed, remapped = canonicalize_scene_speakers(cs, CAST)
    assert changed == 2
    assert remapped == {"The Square": "A SQUARE", "The Narrator": "Narrator"}
    assert [b.speaker for b in cs[0].scenes[0].beats] == [
        "A SQUARE", "Narrator", "The Gatekeeper", None,
    ]


def test_canonicalize_is_idempotent():
    cs = _scenes("The Square", "The Narrator")
    canonicalize_scene_speakers(cs, CAST)
    changed, _ = canonicalize_scene_speakers(cs, CAST)
    assert changed == 0
