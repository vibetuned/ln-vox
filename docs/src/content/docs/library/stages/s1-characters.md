---
title: lnvox.stages.s1_characters
description: Stage 1 character extraction. Per-chapter LLM extraction, sanitisation, deterministic clustering and the LLM merge-groups pass.
sidebar:
  order: 1
  label: s1_characters
---

Stage 1 builds the book's cast (`01_characters.json`). An LLM extracts characters one chapter at a time. A deterministic union-find then collapses duplicates, and one final LLM call only proposes which differently-named clusters are the same person. `lnvox s1` ([stage page](/stages/s1-characters/)) calls `run()`. The clustering helpers are pure functions and are unit-tested without an LLM (`tests/test_s1_clustering.py`, `tests/test_s1_sanitize.py`).

## At a glance

| Symbol | Kind | Purpose |
| --- | --- | --- |
| `run` | function | Stage entry point: extract, sanitise, cluster, merge, write artifacts |
| `extract_per_chapter` | function | One LLM call per chapter to get a `CharacterList` |
| `sanitize_character_list` | function | Drop prompt-example ghosts and pronoun names, canonicalise the narrator |
| `cluster_characters` | function | Deterministic union-find over every per-chapter entry |
| `CharacterCluster` | dataclass | One merged group of source entries |
| `merge_clusters` | function | LLM merge-groups pass, with deterministic fallbacks |
| `_apply_merge_groups` | function | Applies the LLM's groups to the clusters, with a same-gender safety net |
| `NARRATOR_NAME` | constant | `"Narrator"`, the exact spelling the voice matcher special-cases |
| `_FUZZY_NAME_THRESHOLD`, `_FUZZY_MIN_LEN` | constants | `0.9`, `4`: fuzzy name matching knobs |
| `_MERGE_INPUT_CHAR_BUDGET` | constant | `160_000`: above this payload size the LLM merge is skipped |

The types (`Character`, `CharacterList`, `CharacterMergeGroup`, `CharacterMergeProposal`) live in [lnvox.llm.schemas](/library/llm/schemas/). The LLM transport is [`LLMClient`](/library/llm/client/#llmclient).

## API

### `run`

```python
def run(
    chapters: list[Chapter],
    client: LLMClient,
    output_dir: Path,
    *,
    prior_volume_dirs: list[Path] | None = None,
    current_volume_label: str = "",
    on_chapter_done=None,
) -> CharacterList
```

Runs the whole stage for the given chapters and returns the final cast.

1. For each chapter, loads `01_characters_per_chapter/<chapter_id>.json` if it exists and parses. Otherwise it calls `extract_per_chapter` and writes that file. A cache file that fails to parse is re-extracted and overwritten.
2. Passes every per-chapter result, cached or fresh, through `sanitize_character_list` and then calls `on_chapter_done(chapter, sanitized_list)`.
3. Reads `01_characters.json` from each `prior_volume_dirs` entry (`_load_prior_volume_casts`, oldest to newest).
4. If there is more than one chapter, or any prior cast exists, it runs `cluster_characters` and then `merge_clusters`, and writes `01_characters_merge_log.json`. With exactly one chapter and no priors, the sanitised list is the cast as-is, and no merge log is written.
5. Writes `01_characters.json` and returns it.

**Parameters**
- `chapters`: the chapters to process, in order. Each `chapter_id` becomes the cache filename and the cluster origin label. The list must be non-empty. With an empty list, `per_chapter[0]` raises `IndexError`. The CLI refuses an empty selection first.
- `client`: an `LLMClient` pointed at the running server.
- `output_dir`: the book's artifact directory (`artifacts/<book_id>/`). It is created if it doesn't exist.
- `prior_volume_dirs`: earlier volumes of the same series (the CLI passes `lnvox.series.find_prior_volumes`). They are loaded, but they only change control flow: they force the clustering path for a one-chapter book. Their casts are **not** sent to the LLM (see `merge_clusters`).
- `current_volume_label`: accepted and forwarded, but unused.
- `on_chapter_done`: optional `callable(chapter, CharacterList)` progress hook.

**Writes**: `01_characters_per_chapter/<chapter_id>.json` (raw model output, not sanitised), `01_characters_merge_log.json`, `01_characters.json`.

:::caution[A chapter subset overwrites the whole cast]
`01_characters.json` holds the cast of the chapters passed in this call, and nothing else. `lnvox s1 <book> --chapters 03` replaces the whole-book cast with chapter 03's. Re-run without `--chapters` to rebuild it. The per-chapter cache makes that cheap.
:::

### `extract_per_chapter`

```python
def extract_per_chapter(client: LLMClient, chapter: Chapter) -> CharacterList
```

Renders `characters_per_chapter.jinja` with the chapter `title` and `text`, and calls `client.structured(schema=CharacterList, salvage=_salvage_character_list)`. The output budget is `client.budget_for(desired=24576, floor=8192)`: 24K tokens covers a 30+-character cast with evidence, clamped to what the context window leaves after the prompt. Exceptions propagate. That is deliberate: the launcher's retry runs `ensure_vllm` and resumes from the per-chapter cache.

The prompt tells the model to leave out pronoun names, bare role words ("guard", "the maid") and relation words ("dad") from `aliases`, and never to reuse the few-shot example's names. Weak models still do all three. Sanitisation and clustering handle whatever gets through.

### `_salvage_character_list`

```python
def _salvage_character_list(raw: str) -> CharacterList | None
```

This is the `salvage` hook for `LLMClient.structured`, used only after every attempt has failed to parse. A model looping under guided JSON can emit duplicate entries until it hits `max_tokens`, so the array never closes. The hook finds `"characters"`, `"cast_of_characters"` or `"cast"` followed by `[`. It then `raw_decode`s objects one after another until the first incomplete one, keeps those that validate as `Character`, and prints `[salvage] recovered N character(s)…`. It returns `None` when nothing could be recovered. Duplicates in a salvaged list are merged later by clustering.

### `sanitize_character_list`

```python
def sanitize_character_list(cl: CharacterList, chapter_text: str) -> CharacterList
```

Removes three extraction artifacts seen on real books. It checks each entry in this order:

1. **Narrator.** If the normalised name fully matches `(?:the\s+)?narrator` (case-insensitive), the name is rewritten to `NARRATOR_NAME` (`"Narrator"`). The entry is kept even though the word never appears in the prose.
2. **Pronoun names.** If the normalised name is in `_PRONOUNS` (personal, possessive and reflexive forms: `i`, `me`, `he`, `she`, `they`, `themselves`, …), the entry is dropped.
3. **Attestation.** If `_attested_in(chapter_text.lower(), name, aliases)` is false, the entry is dropped. This catches characters copied from the prompt's example ("Sir Patrick").
4. **Duplicates.** A second entry with the same normalised name is dropped. The first one wins.

It runs on fresh **and** cached results. The cache keeps raw model output and sanitisation happens when the result is used, so improving these rules also cleans an existing cache without any LLM calls.

### `_attested_in`

```python
def _attested_in(text_lower: str, name: str, aliases) -> bool
```

Returns true if the name or any alias is actually present in the chapter. For each candidate (lowercased, whitespace-collapsed), it tries:

- **Whole-phrase, word-bounded match:** `\b<candidate>\b`. A bare substring test would let the alias `Pat` match inside `pattern`.
- **All content tokens present:** tokens from `[a-z']+`, minus the stopwords in `_ATTEST_STOPWORDS` (`the`, `a`, `an`, `of`, `and`, `or`, `to`, `in`, `my`, `his`, `her`, `their`, `our`), and minus tokens shorter than `_ATTEST_MIN_TOKEN` (4). If every remaining token appears word-bounded, the candidate passes. This keeps names the extractor composed from description, like "The Four Sons", which passes on `four` + `sons`.

A character the text only ever calls by an alias survives, because aliases are checked too.

### `cluster_characters`

```python
def cluster_characters(
    per_chapter: list[CharacterList], origins: list[str] | None = None
) -> list[CharacterCluster]
```

This is deterministic duplicate collapse, so that the merge LLM sees one compact row per person instead of N copies of every recurring character. `origins[i]` labels the entries of `per_chapter[i]` (the stage passes chapter ids). The default label is `#<i>`.

It is a union-find over every (entry, origin) pair, in three passes.

**1. Collecting identity keys.** An entry's keys are its normalised `name` and every normalised alias (lowercase, whitespace collapsed). Pronoun keys are skipped. For each key the function records which entries carry it, and the set of distinct normalised **names** of those carriers.

**2. Exact pass: union only on identity-safe keys.** A key is *generic*, and never links anyone, when `_names_all_similar(carrier_names)` is false. Otherwise every entry carrying it is unioned. This is what stops "attendant", carried by Alice, Bob and Carol, from chaining three people into one mega-cluster. It still lets "Rosa", carried by `Rosa` and `Lady Rosalind`, link them.

**3. Fuzzy pass: near-identical names only.** For every pair of entries not already in the same cluster, it unions when **all** of these hold:
- both normalised names are at least `_FUZZY_MIN_LEN` (4) characters;
- the lengths differ by at most 3;
- the enumerators are identical (`_enumerators`: every digit run, plus a trailing single-letter or roman-numeral token up to XXXIX, built from `i`/`v`/`x` only so name tokens like `Li` don't count). `Royal Knight 1` / `Royal Knight 2` (0.93), `Attendant A` / `Attendant B` (0.91) and `Guard II` / `Guard III` (0.94) clear the threshold but are never fused;
- `difflib.SequenceMatcher(None, a, b).ratio() >= _FUZZY_NAME_THRESHOLD` (0.9).

Only names are compared in this pass, not aliases. It is O(n²) over all entries.

Each connected component then becomes a `CharacterCluster` (`_build_cluster`):

| Field | Rule |
| --- | --- |
| `canonical` | Most frequent `name` among the members; ties go to the longer name |
| `aliases` | Every member name and alias except the canonical, deduplicated case-insensitively (first casing kept). Excludes pronouns, generic keys from step 2, and any all-lowercase string (`_is_generic_alias`: a real name or epithet has a capital) |
| `gender`, `approx_age` | Majority of member values, ignoring `"unknown"`; `"unknown"` if nothing else |
| `description` | Longest member description |
| `evidence` | Union in first-seen order, capped at 4 |
| `origins` | Origin label of every member, with repeats |
| `member_names` | Sorted distinct member `name`s |

Clusters are returned in prominence order: distinct-chapter count descending (`occurrences`), then canonical name A–Z.

:::tip[Reading a bad merge or a bad split]
- **Two people fused.** Most often both carry a key whose carriers are "similar" by the substring rule. Any name of 4 or more characters inside a longer one counts: `anna` is inside `annabelle`, `rose` inside `rosemary`. The same exact name used for two people always fuses.
- **One person split.** The two names share no identity-safe key and score under 0.9. `tuuli` / `tuli` is only 0.889. `gunther` / `gunter` (0.923) does merge. Such pairs are left to the LLM merge-groups pass.
- Check `merged_from` in `01_characters_merge_log.json` to see exactly which source names went into each cluster.
:::

### `_names_all_similar` and `_similar`

```python
def _similar(a: str, b: str) -> bool
def _names_all_similar(names: set[str]) -> bool
```

`_similar` is true when two normalised names are equal. It is also true when the shorter one (at least 4 characters) is a substring of the longer one, or when both are at least 4 characters, have the same `_enumerators`, and have a `SequenceMatcher` ratio of 0.9 or more. The enumerator guard keeps a key shared by `attendant a` and `attendant b` (such as `attendant`) generic. `_names_all_similar` takes the longest name as the base and requires every other name to be `_similar` to it. A set with 0 or 1 names is trivially similar, so a key carried only by entries with the same `name` is always identity-safe.

### `CharacterCluster`

```python
@dataclass
class CharacterCluster:
    canonical: str
    aliases: list[str]
    gender: str
    approx_age: str
    description: str
    evidence: list[str]
    origins: list[str]
    member_names: list[str] = field(default_factory=list)
```

- `occurrences` (property): `len(set(origins))`, the number of distinct chapters.
- `keys` (property): the normalised canonical name, aliases and member names. `_apply_merge_groups` uses these to resolve names the LLM returns.

### `merge_clusters`

```python
def merge_clusters(
    client: LLMClient,
    clusters: list[CharacterCluster],
    *,
    prior_volume_casts: list[CharacterList] | None = None,
    current_volume_label: str = "",
) -> CharacterList
```

Returns the final cast: the clusters, refined by an LLM pass that **only** proposes groups of differently-named clusters that are the same person. The model never rewrites descriptions or aliases, so nothing it writes enters the cast except the grouping and a preferred name.

- Fewer than 2 clusters: returns `_deterministic_merge(clusters)`, one character per cluster, verbatim.
- It builds the compact payload (`_cluster_payload`: `name`, `aliases`, `gender`, `approx_age`, `chapters` = occurrences, `description[:300]`, `evidence[:2]`). If the JSON is over `_MERGE_INPUT_CHAR_BUDGET` (160 000 characters, about 40K tokens), it skips the LLM and returns the deterministic merge, rather than risk a context-length 400.
- It renders `characters_merge.jinja`. The prompt is conservative: shared roles are not evidence, genders must never differ, and a missed merge is better than a wrong one. It calls `structured(schema=CharacterMergeProposal)` with `budget_for(desired=4096, floor=1024)`.
- An unusable answer (validation failure after the client's retries) falls back to the deterministic merge, silently. A transport error (`is_transport_error`: server unreachable, timeout, 5xx) is **re-raised**, so the step fails and the launcher's `ensure_vllm` retry can restart the server, instead of writing an un-merged `01_characters.json` that s2 would then segment against.
- Otherwise it returns `_apply_merge_groups(clusters, proposal)`.

`prior_volume_casts` and `current_volume_label` are accepted for caller compatibility but deliberately unused. Cross-volume continuity is handled at voice-cast time (DESIGN.md §2.2, [voice cast](/stages/voice-cast/)).

### `_apply_merge_groups`

```python
def _apply_merge_groups(
    clusters: list[CharacterCluster], proposal: CharacterMergeProposal
) -> CharacterList
```

Applies the LLM's groups with a second union-find over clusters.

1. Maps every cluster key to the **first** (most prominent) cluster that carries it.
2. For each `CharacterMergeGroup`, resolves `names` (normalised) to cluster roots, ignoring names it can't resolve. If fewer than 2 distinct roots remain, the group is skipped: unknown names and single-name groups do nothing.
3. **Same-gender safety net.** It collects the non-`unknown` genders of every cluster already in the resolved unions, not just their roots. If there is more than one, the whole group is refused, even if the model insists. So `[Ash (unknown), Brom (male)]` followed by `[Ash, Cora (female)]` keeps Cora separate.
4. Otherwise it unions the roots. `group.canonical` is recorded as a name hint for the merged root (first hint wins).

A single cluster becomes a `Character` verbatim. A multi-cluster group goes through `_combine_clusters`. The canonical name is the hint if it is one of the members' keys, otherwise the member with the most occurrences (ties go to the longer name). Aliases are the union of members' canonical names and aliases, minus pronouns and all-lowercase strings. Gender and age are an **occurrence-weighted** majority, so one mis-tagged chapter can't flip an established trait. Description is the longest, and evidence is capped at 4. The output is sorted by the group's highest occurrence count, then name.

### `_merge_log`

```python
def _merge_log(clusters: list[CharacterCluster], final: CharacterList) -> dict
```

Builds the provenance written to `01_characters_merge_log.json`. A cluster's `disposition` is `kept` when any of its keys matches a final character's name or alias. `dropped` only shows up when a cluster was folded into another and none of its names survived as an alias, for example an all-lowercase canonical name. `new_in_final_unmatched` lists final names that match no cluster. With the merge-groups design it is normally empty. The log records the deterministic clusters, **not** the LLM's merge groups. A cluster the LLM fused shows as `kept`, and its canonical name appears among the survivor's aliases.

## Artifacts

`01_characters_per_chapter/<chapter_id>.json`: the raw `CharacterList` from the model. Sanitisation is not applied to this file.

```json
{
  "characters": [
    {
      "name": "Lady Rosalind",
      "aliases": ["Rosalind", "Rosa", "the lady"],
      "gender": "female",
      "approx_age": "adult",
      "description": "A noblewoman of House Vale who speaks in clipped, formal sentences.",
      "evidence": ["\"You will address me properly,\" Lady Rosalind said."]
    },
    {
      "name": "Sir Patrick",
      "aliases": ["Pat"],
      "gender": "male",
      "approx_age": "elder",
      "description": "Appears in the prompt's example but not the provided text.",
      "evidence": []
    }
  ]
}
```

`01_characters.json`: the final cast, in prominence order. The `Sir Patrick` ghost is gone (unattested), and `the lady` is gone (all-lowercase alias).

```json
{
  "characters": [
    {
      "name": "Lord Vex",
      "aliases": ["Vex", "The Masked Duke"],
      "gender": "male",
      "approx_age": "adult",
      "description": "A masked duke with a slow, cold baritone who rarely raises his voice.",
      "evidence": ["\"You're late,\" Vex said without turning."]
    },
    {
      "name": "Lady Rosalind",
      "aliases": ["Rosalind", "Rosa"],
      "gender": "female",
      "approx_age": "adult",
      "description": "A noblewoman of House Vale who speaks in clipped, formal sentences.",
      "evidence": ["\"You will address me properly,\" Lady Rosalind said."]
    }
  ]
}
```

`01_characters_merge_log.json`: here the LLM fused `The Masked Duke` into `Lord Vex`.

```json
{
  "counts": {"source_clusters": 3, "final_characters": 2},
  "lone_dropped": [],
  "lone_kept": [],
  "new_in_final_unmatched": [],
  "clusters": [
    {"canonical": "Lord Vex", "merged_from": ["Lord Vex", "Vex"],
     "chapters": ["01", "02", "03"], "occurrences": 3, "lone": false, "disposition": "kept"},
    {"canonical": "Lady Rosalind", "merged_from": ["Lady Rosalind", "Rosa", "Rosalind"],
     "chapters": ["01", "03"], "occurrences": 2, "lone": false, "disposition": "kept"},
    {"canonical": "The Masked Duke", "merged_from": ["The Masked Duke"],
     "chapters": ["02", "04"], "occurrences": 2, "lone": false, "disposition": "kept"}
  ]
}
```

## Notes

- **Caching.** Only the per-chapter extraction is cached. Clustering and the merge LLM call run again on every `run()`, so a re-run costs one merge call and may group differently. `01_characters.json` is always overwritten.
- **Durable hand fixes.** Edits to `01_characters.json` are lost the next time s1 runs, and the launcher runs s1 on every narration run. To permanently link two names, add the alias to the relevant `01_characters_per_chapter/<chapter_id>.json` entry, then re-run s1. Clustering will union on it, as long as the key is identity-safe.
- **Narrator.** A first-person book may produce a `Narrator` entry. Keeping that exact spelling is what lets `lnvox voice cast --narrator-clip` reach it (see [voice matcher](/library/voices/matcher/)).
- The field `first_mention_chapter` shown in DESIGN.md §2.2's example JSON is not part of the `Character` schema.
- Design rationale: DESIGN.md §2.2.
