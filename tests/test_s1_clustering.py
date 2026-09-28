"""Regression tests for deterministic character clustering (Stage 1).

The bug these guard against: the per-chapter extractor emits generic role/title
words, relations, and pronouns as aliases. Union-find clustered on ANY shared
key, so a token like "attendant" or "I" — carried by several DISTINCT
characters — chained unrelated people (across genders and ages) into one
mega-cluster. Clustering must only fuse entries whose shared key denotes one
identity (spelling variants of a single name), never a generic shared token.
"""

from lnvox.llm.schemas import (
    Character,
    CharacterList,
    CharacterMergeGroup,
    CharacterMergeProposal,
)
from lnvox.stages.s1_characters import _apply_merge_groups, cluster_characters


def _chapter(*chars: Character) -> CharacterList:
    return CharacterList(characters=list(chars))


def _cluster_of(clusters, name: str):
    for c in clusters:
        members = {m.lower() for m in c.member_names} | {c.canonical.lower()}
        if name.lower() in members:
            return c
    raise AssertionError(f"no cluster contains {name!r}")


def test_generic_role_alias_does_not_chain_distinct_characters():
    # Four distinct people each tagged with the same generic role + pronoun.
    per_chapter = [
        _chapter(
            Character(name="Alice", aliases=["attendant", "she"], gender="female",
                      approx_age="adult", description="An attendant."),
            Character(name="Bob", aliases=["attendant", "he"], gender="male",
                      approx_age="adult", description="An attendant."),
        ),
        _chapter(
            Character(name="Carol", aliases=["attendant", "she"], gender="female",
                      approx_age="teen", description="An attendant."),
            Character(name="Dan", aliases=["the merchant", "he"], gender="male",
                      approx_age="elder", description="A merchant."),
        ),
    ]
    clusters = cluster_characters(per_chapter, origins=["c1", "c2"])
    canon = {c.canonical for c in clusters}
    assert canon == {"Alice", "Bob", "Carol", "Dan"}, canon
    # The generic token must not bleed into anyone's aliases either.
    for c in clusters:
        assert "attendant" not in {a.lower() for a in c.aliases}
        assert "she" not in {a.lower() for a in c.aliases}


def test_first_person_pronoun_does_not_chain_pov_characters():
    # Two different chapters in first person; each POV lists "I".
    per_chapter = [
        _chapter(Character(name="Erin", aliases=["I"], gender="female",
                           approx_age="adult", description="POV ch1.")),
        _chapter(Character(name="Frank", aliases=["I"], gender="male",
                           approx_age="adult", description="POV ch2.")),
    ]
    clusters = cluster_characters(per_chapter, origins=["c1", "c2"])
    assert {c.canonical for c in clusters} == {"Erin", "Frank"}


def test_name_variants_still_merge():
    # A short form, a titled form, and a nickname of one person across chapters,
    # linked the way the extractor records them: the titled entry lists the
    # short forms as aliases.
    per_chapter = [
        _chapter(Character(name="Rosalind", aliases=[], gender="female",
                           approx_age="adult", description="ch1.")),
        _chapter(Character(name="Lady Rosalind", aliases=["Rosalind", "Rosa"],
                           gender="female", approx_age="adult", description="ch2.")),
        _chapter(Character(name="Rosa", aliases=[], gender="female",
                           approx_age="adult", description="ch3.")),
    ]
    clusters = cluster_characters(per_chapter, origins=["c1", "c2", "c3"])
    assert len(clusters) == 1, [c.canonical for c in clusters]
    c = clusters[0]
    assert c.occurrences == 3
    assert {m for m in c.member_names} == {"Rosalind", "Lady Rosalind", "Rosa"}


def _two_distinct_clusters():
    # Two clusters under unrelated names that are actually the same man.
    per_chapter = [
        _chapter(Character(name="Lord Vex", aliases=[], gender="male",
                           approx_age="adult", description="A masked noble. ch1.")),
        _chapter(Character(name="The Masked Duke", aliases=[], gender="male",
                           approx_age="adult", description="A duke in a mask. ch2.")),
        _chapter(Character(name="Mara", aliases=[], gender="female",
                           approx_age="adult", description="A maid. ch3.")),
    ]
    return cluster_characters(per_chapter, origins=["c1", "c2", "c3"])


def test_apply_merge_group_fuses_same_person():
    clusters = _two_distinct_clusters()
    proposal = CharacterMergeProposal(merges=[
        CharacterMergeGroup(canonical="Lord Vex",
                            names=["Lord Vex", "The Masked Duke"]),
    ])
    result = _apply_merge_groups(clusters, proposal)
    names = {c.name for c in result.characters}
    assert names == {"Lord Vex", "Mara"}, names
    vex = next(c for c in result.characters if c.name == "Lord Vex")
    assert "The Masked Duke" in vex.aliases


def test_apply_merge_group_rejects_gender_split():
    clusters = _two_distinct_clusters()
    # Even if the model proposes it, a male+female fuse must be refused.
    proposal = CharacterMergeProposal(merges=[
        CharacterMergeGroup(canonical="Lord Vex", names=["Lord Vex", "Mara"]),
    ])
    result = _apply_merge_groups(clusters, proposal)
    assert {c.name for c in result.characters} == {"Lord Vex", "The Masked Duke", "Mara"}


def test_apply_merge_group_ignores_unknown_and_singletons():
    clusters = _two_distinct_clusters()
    proposal = CharacterMergeProposal(merges=[
        CharacterMergeGroup(canonical="Ghost", names=["Nobody Here", "Lord Vex"]),
        CharacterMergeGroup(canonical="Mara", names=["Mara"]),
    ])
    result = _apply_merge_groups(clusters, proposal)
    # Unresolvable / single-name groups are no-ops.
    assert {c.name for c in result.characters} == {"Lord Vex", "The Masked Duke", "Mara"}


def test_empty_proposal_leaves_cast_unchanged():
    clusters = _two_distinct_clusters()
    result = _apply_merge_groups(clusters, CharacterMergeProposal(merges=[]))
    assert {c.name for c in result.characters} == {"Lord Vex", "The Masked Duke", "Mara"}


def test_shared_distinctive_alias_still_merges():
    # Same person recorded under one name in each chapter, linked by a unique
    # alias only that person ever uses — must still merge.
    per_chapter = [
        _chapter(Character(name="Gregor", aliases=["the Whitebeard"], gender="male",
                           approx_age="elder", description="ch1.")),
        _chapter(Character(name="Gregor", aliases=["the Whitebeard"], gender="male",
                           approx_age="elder", description="ch2.")),
    ]
    clusters = cluster_characters(per_chapter, origins=["c1", "c2"])
    assert len(clusters) == 1
    assert clusters[0].occurrences == 2


def test_merge_groups_check_the_gender_of_the_whole_union():
    # "Ash" (unknown gender) sorts first, so after the first group the union's
    # root is the unknown cluster. The second group must still see the male
    # member behind that root and refuse to fuse the female "Cora" in.
    per_chapter = [
        _chapter(Character(name="Ash", gender="unknown", approx_age="adult",
                           description="A hooded figure. ch1.")),
        _chapter(Character(name="Brom", gender="male", approx_age="adult",
                           description="A smith. ch2.")),
        _chapter(Character(name="Cora", gender="female", approx_age="adult",
                           description="A courier. ch3.")),
    ]
    clusters = cluster_characters(per_chapter, origins=["c1", "c2", "c3"])
    proposal = CharacterMergeProposal(merges=[
        CharacterMergeGroup(canonical="Brom", names=["Ash", "Brom"]),
        CharacterMergeGroup(canonical="Cora", names=["Ash", "Cora"]),
    ])
    result = _apply_merge_groups(clusters, proposal)
    assert {c.name for c in result.characters} == {"Brom", "Cora"}, result
    brom = next(c for c in result.characters if c.name == "Brom")
    assert brom.gender == "male" and "Ash" in brom.aliases


def test_merge_groups_chain_same_gender_through_an_unknown():
    per_chapter = [
        _chapter(Character(name="Ash", gender="unknown", approx_age="adult",
                           description="ch1.")),
        _chapter(Character(name="Brom", gender="male", approx_age="adult",
                           description="ch2.")),
        _chapter(Character(name="Dace", gender="male", approx_age="adult",
                           description="ch3.")),
    ]
    clusters = cluster_characters(per_chapter, origins=["c1", "c2", "c3"])
    proposal = CharacterMergeProposal(merges=[
        CharacterMergeGroup(canonical="Brom", names=["Ash", "Brom"]),
        CharacterMergeGroup(canonical="Brom", names=["Ash", "Dace"]),
    ])
    result = _apply_merge_groups(clusters, proposal)
    assert [c.name for c in result.characters] == ["Brom"]


def _entry(name: str, gender: str = "female", aliases=()) -> CharacterList:
    return _chapter(Character(name=name, aliases=list(aliases), gender=gender,
                              approx_age="adult", description=f"{name}."))


def test_enumerated_role_names_stay_distinct():
    # Each pair clears the 0.9 fuzzy threshold but names two people.
    for a, b in [
        ("Attendant A", "Attendant B"),   # 0.91 — trailing letter
        ("Guard II", "Guard III"),        # 0.94 — roman numeral
        ("Royal Knight 1", "Royal Knight 2"),  # 0.93 — digits
    ]:
        clusters = cluster_characters([_entry(a), _entry(b)], origins=["c1", "c2"])
        assert {c.canonical for c in clusters} == {a, b}, (a, b)


def test_shared_generic_alias_does_not_merge_enumerated_names():
    # Both carry the bare role as an alias; "attendant a"/"attendant b" must
    # not count as variants of one name, so the key stays generic.
    clusters = cluster_characters(
        [_entry("Attendant A", aliases=["attendant"]),
         _entry("Attendant B", aliases=["attendant"])],
        origins=["c1", "c2"],
    )
    assert {c.canonical for c in clusters} == {"Attendant A", "Attendant B"}


def test_spelling_variants_still_fuzzy_merge():
    clusters = cluster_characters(
        [_entry("Gunther", gender="male"), _entry("Gunter", gender="male")],
        origins=["c1", "c2"],
    )
    assert len(clusters) == 1


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok: {fn.__name__}")
    print(f"\n{len(fns)} test(s) passed.")


if __name__ == "__main__":
    _run_all()
