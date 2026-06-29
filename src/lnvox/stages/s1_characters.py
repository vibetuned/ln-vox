import difflib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from lnvox.ingest.text import Chapter
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import Character, CharacterList, CharacterMergeProposal


SYSTEM = (
    "You extract structured character data from novel chapters. "
    "Output a single raw JSON object that matches the requested schema. "
    "Do NOT wrap the output in markdown code fences. "
    "Do NOT include any prose, commentary, or explanation before or after the JSON. "
    "Base every claim strictly on evidence in the provided text."
)


def _salvage_character_list(raw: str) -> CharacterList | None:
    """Recover complete character entries from a truncated/runaway response.

    A weak model under guided JSON can loop, emitting duplicate entries until it
    hits `max_tokens` and the array is never closed. The leading entries are
    still valid and include the chapter's prominent characters (the duplicates
    are deduped later by `cluster_characters`). Walk the `characters` array,
    decoding objects until the first incomplete one, and keep the valid ones.
    """
    m = re.search(r'"(?:characters|cast_of_characters|cast)"\s*:\s*\[', raw)
    if not m:
        return None
    pos = m.end()
    decoder = json.JSONDecoder()
    chars: list[Character] = []
    while pos < len(raw):
        while pos < len(raw) and raw[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(raw) or raw[pos] == "]":
            break
        try:
            obj, pos = decoder.raw_decode(raw, pos)
        except json.JSONDecodeError:
            break  # first incomplete object — stop
        if isinstance(obj, dict):
            try:
                chars.append(Character.model_validate(obj))
            except Exception:
                continue
    if not chars:
        return None
    print(
        f"  [salvage] recovered {len(chars)} character(s) from a truncated "
        f"response (run-away generation; duplicates will be deduped in merge)"
    )
    return CharacterList(characters=chars)


def extract_per_chapter(client: LLMClient, chapter: Chapter) -> CharacterList:
    user = client.render(
        "characters_per_chapter.jinja",
        title=chapter.title,
        text=chapter.text,
    )
    # Per-chapter casts vary wildly — a chamber drama has 3-5 characters; a
    # dungeon raid or court scene can have 30+. 24K output tokens (~80K chars of
    # JSON) covers even the largest cast with evidence; `budget_for` clamps it
    # down if the chapter text leaves less context room. Generous enough to
    # avoid mid-JSON truncation, bounded enough to keep the request timeout sane.
    budget = client.budget_for(
        system=SYSTEM, user=user, desired=24576, floor=8192
    )
    return client.structured(
        system=SYSTEM,
        user=user,
        schema=CharacterList,
        max_tokens=budget,
        salvage=_salvage_character_list,
    )


def _load_prior_volume_casts(prior_volume_dirs: list[Path]) -> list[CharacterList]:
    """Read `01_characters.json` from each prior volume directory, oldest → newest."""
    out: list[CharacterList] = []
    for d in prior_volume_dirs:
        path = d / "01_characters.json"
        if path.exists():
            out.append(CharacterList.model_validate_json(
                path.read_text(encoding="utf-8")
            ))
    return out


# ---------------- deterministic clustering ----------------
#
# Sending every per-chapter list (full descriptions + evidence) to the merge
# LLM blows the context window: a character recurring across N chapters costs
# ~N× its JSON, and a long volume easily exceeds the model's input budget. So
# we first collapse obvious duplicates DETERMINISTICALLY — entries that share a
# name or alias, or whose names are near-identical — and hand the LLM only one
# compact summary per cluster.

# Fuzzy name match: high cutoff + min length so we don't fuse short distinct
# names ("Mark"/"Marx"). Catches transliteration / typo variants ("Gunther"/
# "Gunter", "Tuuli"/"Tuli").
_FUZZY_NAME_THRESHOLD = 0.9
_FUZZY_MIN_LEN = 4

# If the compact clustered payload still exceeds this many characters
# (~40K tokens), skip the LLM and return the deterministic merge rather than
# risk a context-length 400 from the model server.
_MERGE_INPUT_CHAR_BUDGET = 160_000


# Pronouns are never identity keys. A first-person POV entry routinely lists
# "I" (or "he"/"she") as an alias; clustering on those would chain every POV
# character in the book into one blob. Personal/possessive/reflexive forms
# only — kept tight so we never shadow a real name.
_PRONOUNS = {
    "i", "me", "my", "mine", "myself",
    "you", "your", "yours", "yourself", "yourselves",
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "it", "its", "itself",
    "we", "us", "our", "ours", "ourselves",
    "they", "them", "their", "theirs", "themselves",
}


def _norm(s: str) -> str:
    return " ".join(s.lower().strip().split())


def _is_generic_alias(name: str) -> bool:
    """Whether a name is a generic descriptor rather than a proper name/epithet.

    A proper name or distinctive epithet always carries a capitalized word
    ("Miss Myne", "the Saint", "High Bishop"); a bare role/relation descriptor
    ("the girl", "merchant", "attendant", "little brother") is all lowercase.
    Used to keep such descriptors out of a character's alias list — they're
    noise for casting and speaker-tagging, and (being shared) cause confusion.
    The character's primary `name` is never filtered this way, only aliases.
    """
    return not any(ch.isupper() for ch in name)


def _digits(s: str) -> list[str]:
    """Runs of digits in a string, e.g. 'Knight 12' → ['12']."""
    return re.findall(r"\d+", s)


def _similar(a: str, b: str) -> bool:
    """Whether two normalized names are variants of the SAME underlying name.

    True when one contains the other (e.g. a short form inside a longer
    titled form) or they're fuzzily near-identical (transliteration / typo
    variants). Short tokens are excluded from the substring path so we don't
    treat an incidental fragment as a name match.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= _FUZZY_MIN_LEN and short in long:
        return True
    if min(len(a), len(b)) >= _FUZZY_MIN_LEN:
        if difflib.SequenceMatcher(None, a, b).ratio() >= _FUZZY_NAME_THRESHOLD:
            return True
    return False


def _names_all_similar(names: set[str]) -> bool:
    """Whether a shared name/alias key denotes ONE identity vs a generic token.

    Variants of a single name (a short form, a titled form, a nickname) are all
    similar to the longest form, so the key is a safe identity key. A generic
    role/title/relation shared by DIFFERENT people (e.g. "attendant",
    "merchant", "the maid" carried by several distinct names) yields dissimilar
    names — that key must NOT cluster them together.
    """
    names = {n for n in names if n}
    if len(names) <= 1:
        return True
    base = max(names, key=len)
    return all(_similar(base, n) for n in names if n != base)


def _majority(values: list[str]) -> str:
    counts = Counter(v for v in values if v and v != "unknown")
    return counts.most_common(1)[0][0] if counts else "unknown"


@dataclass
class CharacterCluster:
    """One deterministically-merged group of source character entries."""

    canonical: str
    aliases: list[str]
    gender: str
    approx_age: str
    description: str  # representative (longest source description)
    evidence: list[str]
    origins: list[str]  # chapter ids each member came from (with repeats)
    member_names: list[str] = field(default_factory=list)

    @property
    def occurrences(self) -> int:
        return len(set(self.origins))

    @property
    def keys(self) -> set[str]:
        return (
            {_norm(self.canonical)}
            | {_norm(a) for a in self.aliases}
            | {_norm(m) for m in self.member_names}
        ) - {""}


def _build_cluster(
    chars: list[Character],
    origins: list[str],
    drop_keys: frozenset[str] = frozenset(),
) -> CharacterCluster:
    name_counts = Counter(c.name for c in chars)
    canonical = max(name_counts, key=lambda nm: (name_counts[nm], len(nm)))
    canon_key = _norm(canonical)

    # Aliases = union of every name/alias form except the canonical, deduped
    # case-insensitively (keeping the first-seen casing). Pronouns and generic
    # tokens shared across distinct characters (`drop_keys`) are excluded so the
    # final cast doesn't carry role/title noise into casting and speaker-tagging.
    alias_repr: dict[str, str] = {}
    for c in chars:
        for nm in [c.name, *c.aliases]:
            k = _norm(nm)
            if (
                k
                and k != canon_key
                and k not in _PRONOUNS
                and k not in drop_keys
                and not _is_generic_alias(nm)
            ):
                alias_repr.setdefault(k, nm)

    evidence: list[str] = []
    seen_ev: set[str] = set()
    for c in chars:
        for e in c.evidence:
            if e not in seen_ev:
                seen_ev.add(e)
                evidence.append(e)

    return CharacterCluster(
        canonical=canonical,
        aliases=list(alias_repr.values()),
        gender=_majority([c.gender for c in chars]),
        approx_age=_majority([c.approx_age for c in chars]),
        description=max((c.description for c in chars), key=len, default=""),
        evidence=evidence[:4],
        origins=origins,
        member_names=sorted({c.name for c in chars}),
    )


def cluster_characters(
    per_chapter: list[CharacterList], origins: list[str] | None = None
) -> list[CharacterCluster]:
    """Collapse per-chapter character entries into clusters by name/alias.

    Union-find: entries are merged when they share any normalized name/alias
    key, and additionally when their normalized names are fuzzily near-equal.
    """
    entries: list[tuple[Character, str]] = []
    for i, cl in enumerate(per_chapter):
        origin = origins[i] if origins else f"#{i}"
        for c in cl.characters:
            entries.append((c, origin))

    n = len(entries)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Exact: share a name/alias key — but ONLY on keys that safely denote a
    # single identity. The per-chapter extractor emits generic role/title words
    # ("attendant", "the merchant", "guard"), relations ("dad"), and pronouns as
    # aliases; unioning on those chains unrelated people (across genders and
    # ages) into one mega-cluster. So we first collect, per key, the distinct
    # source canonical names carrying it, and union only on keys whose carriers
    # are spelling variants of one name (`_names_all_similar`) — never on a token
    # shared by genuinely different characters.
    key_entries: dict[str, list[int]] = defaultdict(list)
    key_names: dict[str, set[str]] = defaultdict(set)
    for idx, (c, _) in enumerate(entries):
        keys = ({_norm(c.name)} | {_norm(a) for a in c.aliases}) - {""}
        for k in keys:
            if k in _PRONOUNS:
                continue
            key_entries[k].append(idx)
            key_names[k].add(_norm(c.name))
    # Keys shared across genuinely different characters are generic tokens
    # (roles, titles, relations). They never link an identity and shouldn't
    # surface as anyone's alias — drop them when building clusters below.
    generic_keys = frozenset(
        k for k, names in key_names.items() if not _names_all_similar(names)
    )
    for k, idxs in key_entries.items():
        if len(idxs) < 2 or k in generic_keys:
            continue
        for other in idxs[1:]:
            union(idxs[0], other)

    # Fuzzy: near-identical canonical names not already in the same cluster.
    norm_names = [_norm(c.name) for c, _ in entries]
    for i in range(n):
        ni = norm_names[i]
        if len(ni) < _FUZZY_MIN_LEN:
            continue
        for j in range(i + 1, n):
            nj = norm_names[j]
            if len(nj) < _FUZZY_MIN_LEN or abs(len(ni) - len(nj)) > 3:
                continue
            if find(i) == find(j):
                continue
            # Never fuse enumerated names whose digits differ — "Knight 1" and
            # "Knight 2" are 0.9-similar but distinct people.
            if _digits(ni) != _digits(nj):
                continue
            if difflib.SequenceMatcher(None, ni, nj).ratio() >= _FUZZY_NAME_THRESHOLD:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        groups[find(idx)].append(idx)

    clusters = [
        _build_cluster(
            [entries[m][0] for m in members],
            [entries[m][1] for m in members],
            drop_keys=generic_keys,
        )
        for members in groups.values()
    ]
    clusters.sort(key=lambda c: (-c.occurrences, c.canonical.lower()))
    return clusters


def _cluster_payload(clusters: list[CharacterCluster]) -> str:
    """Compact per-cluster summary for the LLM — capped description, ≤2 quotes."""
    return json.dumps(
        [
            {
                "name": c.canonical,
                "aliases": c.aliases,
                "gender": c.gender,
                "approx_age": c.approx_age,
                "chapters": c.occurrences,
                "description": c.description[:300],
                "evidence": c.evidence[:2],
            }
            for c in clusters
        ],
        ensure_ascii=False,
        indent=2,
    )


def _cluster_to_character(c: CharacterCluster) -> Character:
    return Character(
        name=c.canonical,
        aliases=c.aliases,
        gender=c.gender,
        approx_age=c.approx_age,
        description=c.description,
        evidence=c.evidence,
    )


def _deterministic_merge(clusters: list[CharacterCluster]) -> CharacterList:
    """The clean base cast: one character per cluster, verbatim."""
    return CharacterList(characters=[_cluster_to_character(c) for c in clusters])


def _combine_clusters(
    members: list[CharacterCluster], canonical_hint: str = ""
) -> Character:
    """Fuse clusters the LLM judged to be one person into a single character.

    Every field value comes from our own clean cluster data — the LLM
    contributes only the grouping decision and the preferred name. Gender/age
    use occurrence-weighted majority so one mis-tagged chapter can't flip a
    well-established trait.
    """
    member_keys = set().union(*(m.keys for m in members))
    if canonical_hint and _norm(canonical_hint) in member_keys:
        canonical = canonical_hint
    else:
        canonical = max(
            members, key=lambda m: (m.occurrences, len(m.canonical))
        ).canonical
    canon_key = _norm(canonical)

    aliases: list[str] = []
    seen = {canon_key}
    for m in members:
        for nm in [m.canonical, *m.aliases]:
            k = _norm(nm)
            if k and k not in seen and k not in _PRONOUNS and not _is_generic_alias(nm):
                seen.add(k)
                aliases.append(nm)

    genders = [m.gender for m in members for _ in range(max(1, m.occurrences))]
    ages = [m.approx_age for m in members for _ in range(max(1, m.occurrences))]

    evidence: list[str] = []
    seen_ev: set[str] = set()
    for m in members:
        for e in m.evidence:
            if e not in seen_ev:
                seen_ev.add(e)
                evidence.append(e)

    return Character(
        name=canonical,
        aliases=aliases,
        gender=_majority(genders),
        approx_age=_majority(ages),
        description=max((m.description for m in members), key=len, default=""),
        evidence=evidence[:4],
    )


def _apply_merge_groups(
    clusters: list[CharacterCluster], proposal: CharacterMergeProposal
) -> CharacterList:
    """Apply the LLM's same-person groupings to our clean clusters.

    Only groups that resolve to 2+ distinct clusters and don't fuse a known
    gender split are honored; everything else stays its own character. Output
    keeps prominence order (most chapters first).
    """
    key_to_idx: dict[str, int] = {}
    for i, c in enumerate(clusters):
        for k in c.keys:
            key_to_idx.setdefault(k, i)

    parent = list(range(len(clusters)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    hints: dict[int, str] = {}
    for g in proposal.merges:
        roots = sorted(
            {find(key_to_idx[_norm(nm)]) for nm in g.names if _norm(nm) in key_to_idx}
        )
        if len(roots) < 2:
            continue  # nothing real to merge (unknown or single name)
        known = {clusters[r].gender for r in roots if clusters[r].gender != "unknown"}
        if len(known) > 1:
            continue  # safety net: never fuse a known gender split
        for other in roots[1:]:
            union(roots[0], other)
        if g.canonical:
            hints.setdefault(find(roots[0]), g.canonical)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(clusters)):
        groups[find(i)].append(i)

    items: list[tuple[int, Character]] = []
    for root, members in groups.items():
        cl = [clusters[i] for i in members]
        char = (
            _cluster_to_character(cl[0])
            if len(cl) == 1
            else _combine_clusters(cl, hints.get(root, ""))
        )
        items.append((max(c.occurrences for c in cl), char))
    items.sort(key=lambda t: (-t[0], t[1].name.lower()))
    return CharacterList(characters=[c for _, c in items])


def merge_clusters(
    client: LLMClient,
    clusters: list[CharacterCluster],
    *,
    prior_volume_casts: list[CharacterList] | None = None,
    current_volume_label: str = "",
) -> CharacterList:
    """Final cast = the clean deterministic clusters, refined by an LLM pass
    that ONLY proposes which differently-named clusters are the same person.

    The model sees just this volume's compact cluster list (no prior volumes,
    no descriptions to rewrite) and returns merge GROUPS, which we apply to our
    own clusters. This does the cross-name fusion judgment clustering can't,
    without the failure modes of a full-cast rewrite (reintroduced generic
    aliases, runaway repetition) or the prior-volume context blow-up.
    `prior_volume_casts` is accepted for caller compatibility but intentionally
    unused — cross-volume continuity is handled at voice-cast time.
    """
    if len(clusters) < 2:
        return _deterministic_merge(clusters)

    clustered_json = _cluster_payload(clusters)
    if len(clustered_json) > _MERGE_INPUT_CHAR_BUDGET:
        return _deterministic_merge(clusters)

    user = client.render("characters_merge.jinja", clustered_cast_json=clustered_json)
    # Output is just a handful of name groups — small and bounded, so a tight
    # budget is plenty and keeps a weak model from running away.
    budget = client.budget_for(system=SYSTEM, user=user, desired=4096, floor=1024)
    try:
        proposal = client.structured(
            system=SYSTEM, user=user, schema=CharacterMergeProposal, max_tokens=budget
        )
    except Exception:
        return _deterministic_merge(clusters)

    return _apply_merge_groups(clusters, proposal)


def _merge_log(clusters: list[CharacterCluster], final: CharacterList) -> dict:
    """Provenance: what merged into each cluster + each cluster's disposition."""
    final_keys = [
        ({_norm(fc.name)} | {_norm(a) for a in fc.aliases}) - {""}
        for fc in final.characters
    ]

    clusters_log = []
    for c in clusters:
        kept = any(c.keys & fk for fk in final_keys)
        clusters_log.append(
            {
                "canonical": c.canonical,
                "merged_from": c.member_names,
                "chapters": sorted(set(c.origins)),
                "occurrences": c.occurrences,
                "lone": c.occurrences == 1,
                "disposition": "kept" if kept else "dropped",
            }
        )

    new_in_final = [
        fc.name
        for fc, fk in zip(final.characters, final_keys)
        if not any(c.keys & fk for c in clusters)
    ]
    return {
        "counts": {
            "source_clusters": len(clusters),
            "final_characters": len(final.characters),
        },
        "lone_dropped": [c["canonical"] for c in clusters_log if c["lone"] and c["disposition"] == "dropped"],
        "lone_kept": [c["canonical"] for c in clusters_log if c["lone"] and c["disposition"] == "kept"],
        "new_in_final_unmatched": new_in_final,
        "clusters": clusters_log,
    }


def run(
    chapters: list[Chapter],
    client: LLMClient,
    output_dir: Path,
    *,
    prior_volume_dirs: list[Path] | None = None,
    current_volume_label: str = "",
    on_chapter_done=None,
) -> CharacterList:
    output_dir.mkdir(parents=True, exist_ok=True)
    per_chapter_dir = output_dir / "01_characters_per_chapter"
    per_chapter_dir.mkdir(exist_ok=True)

    per_chapter: list[CharacterList] = []
    for ch in chapters:
        # Per-chapter results are deterministic. If we already have one on
        # disk from a previous attempt (e.g. s1 crashed at chapter 10), load
        # it back instead of re-calling the LLM. This makes retry cheap.
        cached_path = per_chapter_dir / f"{ch.chapter_id}.json"
        if cached_path.exists():
            try:
                result = CharacterList.model_validate_json(
                    cached_path.read_text(encoding="utf-8")
                )
            except Exception:
                # File exists but is malformed (e.g. truncated). Re-extract.
                result = extract_per_chapter(client, ch)
                cached_path.write_text(
                    result.model_dump_json(indent=2), encoding="utf-8"
                )
        else:
            result = extract_per_chapter(client, ch)
            cached_path.write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        per_chapter.append(result)
        if on_chapter_done:
            on_chapter_done(ch, result)

    prior_casts = _load_prior_volume_casts(prior_volume_dirs or [])

    if len(per_chapter) > 1 or prior_casts:
        clusters = cluster_characters(
            per_chapter, origins=[ch.chapter_id for ch in chapters]
        )
        merged = merge_clusters(
            client,
            clusters,
            prior_volume_casts=prior_casts,
            current_volume_label=current_volume_label,
        )
        (output_dir / "01_characters_merge_log.json").write_text(
            json.dumps(_merge_log(clusters, merged), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        merged = per_chapter[0]

    (output_dir / "01_characters.json").write_text(
        merged.model_dump_json(indent=2), encoding="utf-8"
    )
    return merged
