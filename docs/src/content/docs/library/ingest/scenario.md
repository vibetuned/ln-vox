---
title: lnvox.ingest.scenario
description: Theater-script markdown to 00_script.json. Header chunking, LLM structuring, verbatim validation, speaker merging, cast extraction, content-keyed cache.
sidebar:
  order: 5
  label: scenario
---

`lnvox.ingest.scenario` is scenario mode's Stage 0 (DESIGN.md §17.2–17.3). It reads a theater script exported to markdown and produces a structured script in which every line is `dialogue`, `staging` or `cue`, with dialogue text kept **verbatim**. It also writes the casting roster that `voice cast` needs. Scene boundaries are found deterministically. An LLM does the per-line classification, and code checks that the LLM did not rewrite anything. The only caller is [`lnvox ingest-scenario`](/stages/ingest-scenario/). [`stages.scenario`](/library/stages/scenario/) imports `is_group_label` from here.

Source: `src/lnvox/ingest/scenario.py`. Prompts: `scenario_structure.jinja`, `scenario_roster.jinja`, `scenario_characters.jinja` in `src/lnvox/llm/prompts/`.

:::note[IP rule]
Script content is sent only to the locally served LLM. No real script text may appear in code, prompts or tests (DESIGN.md §17). All examples on this page are invented.
:::

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`run`](#run) | function | Whole ingest: markdown → `00_script.json`, `00_text.jsonl`, `01_characters.json` |
| [`chunk_script`](#chunk_script) | function | Split into roster chunks + `(scene_title, scene_text)` chunks |
| [`is_scene_header`](#is_scene_header) / [`is_roster_header`](#is_roster_header) | functions | The deterministic header detectors |
| [`split_chunk`](#split_chunk) | function | Split an overlong scene at blank lines (`_MAX_CHUNK_CHARS = 7000`) |
| [`validate_items`](#validate_items) | function | The verbatim invariant: failing dialogue is demoted to staging |
| [`canonicalize_speakers`](#canonicalize_speakers) | function | Merge speaker-label variants into one name per character |
| [`is_group_label`](#is_group_label) | function | "Tous", "All", … are never cast |
| [`cached_structured`](#cached_structured) | function | `client.structured` behind the `cache/scenario/` disk cache |
| [`structure_chunk`](#structure_chunk) / [`extract_roster`](#extract_roster) / [`extract_characters`](#extract_characters) | functions | The three LLM passes |
| [`clean_md_line`](#clean_md_line) / [`norm_ws`](#norm_ws) | functions | Markdown unescaping and whitespace collapsing |

## API

### `run`

```python
def run(
    md_path: Path,
    scenario_id: str,
    client: LLMClient,
    out_dir: Path,
    *,
    title: str = "",
    language: str = "fr",
    cache_dir: Path | None = Path("cache") / "scenario",
    refresh_cache: bool = False,
    progress: Callable[[str], None] = print,
) -> ScenarioScript
```

Structures one script into `out_dir`. In order:

1. `chunk_script(md_text)` → roster chunks and scene chunks. Logs `N scene(s), M roster section(s) detected`.
2. **Roster**: one [`extract_roster`](#extract_roster) call per roster chunk, logging `roster: N entrie(s)`.
3. **Scenes**: `scene_id = f"{i + 1:02d}"` in chunk order. Each scene is [`split_chunk`](#split_chunk)ed. Each part is [`structure_chunk`](#structure_chunk)ed and then [`validate_items`](#validate_items)ed against **that part's** text. Logs `✓ scene 03: 41 item(s), 29 dialogue line(s)`, with ` (cached)` appended when every part came from the cache.
4. If any dialogue was demoted: `[warn] N dialogue line(s) failed verbatim validation and were kept as staging — review 00_script.json`.
5. [`canonicalize_speakers`](#canonicalize_speakers) over every dialogue speaker in the whole script, applied in place.
6. [`extract_characters`](#extract_characters), the last LLM pass, runs **before anything is written**.
7. **Writes `00_text.jsonl`**: one [`Chapter`](/library/ingest/text/#chapter) per scene, so s5 gets m4b chapter markers. `chapter_id = scene_id`, `title = scene.title or f"Scène {scene_id}"`, and `text` is the dialogue and staging texts joined by blank lines (cues excluded).
8. **Writes `01_characters.json`** (atomically, via a temp file + rename), logging `✓ N character(s) for casting`.
9. **Writes `00_script.json`** (`ScenarioScript`, indent 2) **last**, atomically, so it only appears once the other two are in place. The launcher treats it plus `01_characters.json` as "ingest done".
10. With a cache: `LLM cache: H/N call(s) served from cache/scenario/`. `N` counts one extra call for the characters pass, and `H` never counts that pass's hit.

**Parameters**
- `md_path`: the script markdown (UTF-8).
- `scenario_id`: stored as `scenario_id`. The CLI passes `--id`.
- `client`: an [`LLMClient`](/library/llm/client/) pointed at a running server.
- `out_dir`: `artifacts/<id>/` (created if missing).
- `title`: play title. Default `norm_ws(clean_md_line(md_path.stem))`, i.e. the markdown-cleaned file name.
- `language`: stored in `00_script.json`. No later stage reads it; the voicebank language is chosen with `LNVOX_VOICEBANK`.
- `cache_dir`: LLM cache directory, **relative to the working directory**. `None` disables caching entirely: it is neither read nor written.
- `refresh_cache`: re-roll every LLM call (skip the cache read) and overwrite the cached entries with the fresh results. The CLI sets it from `--no-cache`.
- `progress`: log sink.

**Returns** the `ScenarioScript` it wrote.

**Raises** whatever `LLMClient.structured` raises: `ValueError` after its schema-validation attempts, or `openai` connection/timeout errors. Nothing is caught. Files are written only at steps 7–9, after every LLM pass (including the characters pass) has succeeded, so an LLM failure writes nothing: files from an earlier successful run are left untouched. Cached chunks survive, though, and a re-run resumes from them.

#### Outputs

```json title="artifacts/my-play/00_script.json"
{
  "scenario_id": "my-play",
  "title": "my-play",
  "language": "fr",
  "roster": [
    {"name": "Gardien", "description": "", "actor": "Paul"},
    {"name": "Le facteur", "description": "", "actor": "Paul"},
    {"name": "Voyageuse", "description": "jeune femme pressée, arrivée de nuit", "actor": "Jeanne"}
  ],
  "scenes": [
    {
      "scene_id": "01",
      "title": "Séquence 1 – La porte",
      "items": [
        {"type": "dialogue", "speaker": "Gardien", "text": "Qui va là ?"},
        {"type": "staging", "speaker": "", "text": "Un volet claque au premier étage."},
        {"type": "cue", "speaker": "", "text": "SON 3"},
        {"type": "dialogue", "speaker": "Voyageuse", "text": "C'est moi, ouvrez !"}
      ]
    },
    {
      "scene_id": "02",
      "title": "2. LE MATIN",
      "items": [
        {"type": "dialogue", "speaker": "Gardien", "text": "Vous voilà enfin."}
      ]
    }
  ]
}
```

```json title="artifacts/my-play/00_text.jsonl"
{"chapter_id":"01","title":"Séquence 1 – La porte","text":"Qui va là ?\n\nUn volet claque au premier étage.\n\nC'est moi, ouvrez !"}
{"chapter_id":"02","title":"2. LE MATIN","text":"Vous voilà enfin."}
```

```json title="artifacts/my-play/01_characters.json"
{
  "characters": [
    {
      "name": "Gardien",
      "aliases": [],
      "gender": "male",
      "approx_age": "adult",
      "description": "Veilleur méfiant, voix posée et grave.",
      "evidence": []
    },
    {
      "name": "Voyageuse",
      "aliases": [],
      "gender": "female",
      "approx_age": "young_adult",
      "description": "Jeune femme pressée, énergique et un peu essoufflée.",
      "evidence": []
    }
  ]
}
```

The schemas (`ScenarioScript`, `ScriptScene`, `ScriptItem`, `RosterEntry`, `CharacterList`) are documented on [`lnvox.llm.schemas`](/library/llm/schemas/).

### `chunk_script`

```python
def chunk_script(md_text: str) -> tuple[list[str], list[tuple[str, str]]]
```

A single line-by-line pass with three states: *preamble*, *in a scene*, *in a roster section*.

- A line that is a [roster header](#is_roster_header) (and not also a scene header) closes the current chunk and opens a roster section. Everything up to the next scene header goes into that roster chunk.
- A line that is a [scene header](#is_scene_header) closes the current chunk and opens a scene. The title is the header with markdown and leading `#`s removed and whitespace collapsed (`### 2. LE MATIN` → `2. LE MATIN`). The header line itself is not part of the chunk.
- Lines before the first header of either kind are the **preamble**.
- Chunks whose body is empty are discarded, so scene numbering has no gaps.
- **No scene header at all** → one scene titled `""` holding everything except the cast list: the preamble plus whatever follows the cast list. With no scene header to end it, a roster section is cut after its first block of lines under the heading, extended across blank lines only while the next block is a markdown list item (`- `, `* `, `+ `, `1. `). The first other block (typically the first `**Name** - line`) starts the play.

:::caution[What gets lost]
- When at least one scene header exists, the **preamble is dropped**: title page, cue legend, and anything else before the first header.
- With no scene headers, a cast list written as blank-line-separated paragraphs that are not list items keeps only its first paragraph as roster; the other entries are structured as play text (they come out as staging or dialogue items, verbatim, not lost).
- A heading that is not a scene header by the rules below (`## Prologue`, `Scene one`) does not start a new scene. Its lines join the previous scene, or the preamble.

If `ingest-scenario` reports `1 scene(s)` for a multi-scene play, the headers were not recognised. Check them against `_HEADER_PATTERNS`.
:::

### `is_scene_header`

```python
def is_scene_header(line: str) -> bool
```

True if `line.strip()` matches any of `_HEADER_PATTERNS`:

| Pattern | Matches |
|---|---|
| `^#{1,6}\s*\d+\s*[.\\)]?\s*\S` | markdown heading that starts with a number: `### 2. LE MATIN`, `## 3) Le soir` |
| `(?i)^\s*\**\s*(s[ée]quence\|sc[èe]ne\|acte?\|p[ée]riode\|tableau\|partie)\s+[\divxlc]+` | keyword + number or roman numeral: `Séquence 1 – La porte`, `**Scène 3**`, `ACTE II`, `Act I`, `Tableau 4`, `Partie III` |
| `^\*\*\s*\d+\s*\\?\.?\s*\*\*\s*$` | a bold number alone on the line: `**3**`, `**12\.**` |
| `^\*{0,2}\s*\d+\s*[–—-]\s+\S` | number + dash: `3 – LE PORT`, `**3 - Title**` |

The keyword pattern only needs the next word to *start* with a digit or one of `i v x l c`. So spelled-out numbers (`Scène deux`) are missed, while a staging line such as `Tableau vivant…` or `Scène vide, la lumière baisse.` is taken for a header.

### `is_roster_header`

```python
def is_roster_header(line: str) -> bool
```

The markdown-cleaned line, with leading `#`s removed, must match

```python
r"(?i)^\s*(les\s+)?(personnages?|characters?|cast|distribution|dramatis\s+person(ae|æ))\b.{0,40}$"
```

**and** be heading-shaped: it starts with `#`, is fully bold (`**…**`), is all upper-case, or has at most two words. `### Personnages`, `CHARACTERS`, `**Distribution**` and `Cast` qualify. `Characters enter the stage slowly from the left side` does not.

### `split_chunk`

```python
def split_chunk(chunk: str, max_chars: int = _MAX_CHUNK_CHARS) -> list[str]
```

A chunk of at most `max_chars` (7000) is returned whole. A longer one is split at blank-line boundaries (`\n\s*\n`) into parts that each stay under the limit, so the model never sees a truncated line. A single block longer than the limit is kept intact.

### `validate_items`

```python
def validate_items(items: list[ScriptItem], chunk: str) -> tuple[list[ScriptItem], int]
```

Enforces the verbatim invariant and returns `(validated_items, n_demoted)`.

- **Haystack**: every line of the chunk passed through [`clean_md_line`](#clean_md_line), joined with spaces, whitespace-collapsed and **lowercased**.
- For each item, `text` is whitespace-collapsed first. Items with empty text are dropped.
- A `dialogue` item whose lowercased text is **not a substring** of the haystack becomes `ScriptItem(type="staging", speaker="", text=text)` and is counted. The text is kept, never dropped, so a human can review it.
- Every other item is kept, with `text` and `speaker` whitespace-collapsed.

What the check does and does not guarantee:
- It is case-insensitive, and line breaks in the source do not matter, because lines are joined with spaces.
- Only **dialogue** is checked. Staging and cue text is trusted as returned.
- It is a substring test, not an alignment. Nothing detects a line the LLM **omitted**, duplicated or reordered. To audit coverage, diff the dialogue in `00_script.json` against the source.
- DESIGN.md §17.2 mentions a "per-chunk retry" before the per-line fallback. No verbatim-driven retry exists: a failing line is demoted immediately. `LLMClient.structured`'s own retries only cover JSON/schema failures.

### `canonicalize_speakers`

```python
def canonicalize_speakers(labels: list[str]) -> dict[str, str]
```

Maps every raw label to one canonical form per character.

1. Clean each label: markdown removed, trailing spaces/`-`/`–`/`:` stripped, whitespace collapsed.
2. Group the cleaned forms by stem ([`_label_stem`](#_label_stem): parentheticals removed, lowercased).
3. Within a stem, the canonical form is the most frequent cleaned variant, with ties going to the longer one.

`Rôle 1 (Prénom)`, `Rôle 1(Prénom)` and `Rôle 1` therefore all become `Rôle 1 (Prénom)`.

Two different characters whose labels differ **only** in the parenthetical (`Garde (Paul)` and `Garde (Marc)`) have the same stem and are merged into one character.

### `is_group_label`

```python
def is_group_label(label: str) -> bool
```

True when the label's stem is one of `tous`, `toutes`, `tous ensemble`, `ensemble`, `all`, `choeur`. The match is exact after stemming: `Chœur` with the ligature does not match. Group lines are excluded from casting and rendered with the Narrator voice (DESIGN.md §17.6).

### `_label_stem`

```python
def _label_stem(label: str) -> str
```

`clean_md_line`, then parentheticals `(…)` removed, trailing separators removed, whitespace collapsed, lowercased. This is the identity key used by canonicalization, group detection, and roster-to-speaker matching.

### `clean_md_line`

```python
def clean_md_line(line: str) -> str
```

Removes the backslash escapes that docx→markdown exporters add (`\-`, `\!`, `\.`, `\+`, `\(`, `\«`, …). It also removes every `**` and every single-`*` emphasis marker, then strips the line. `**Gardien** \- Qui va là ?` → `Gardien - Qui va là ?`.

### `norm_ws`

```python
def norm_ws(s: str) -> str
```

`" ".join(s.split())`.

### `cached_structured`

```python
def cached_structured(
    client,
    *,
    cache_dir: Path | None,
    system: str,
    user: str,
    schema,
    desired: int,
    floor: int,
    refresh: bool = False,
)
```

`client.structured` behind a content-addressed disk cache. It returns `(result, was_cache_hit)`.

- **Key**: `_cache_key(schema.__name__, system, user, client.settings.llm.model)`, i.e. SHA-256 over the parts, each followed by `\x1f`, truncated to 16 hex characters. The file is `<cache_dir>/<key>.json`. (The system prompt joined the key in the 2026-09 fix round, so entries written before then miss once and are re-paid.)
- **Hit**: the file parses with `schema.model_validate_json`. A corrupt or truncated entry is regenerated.
- **Miss**: `budget = client.budget_for(system=…, user=…, desired=…, floor=…)`, then `client.structured(…, max_tokens=budget)`. The **raw LLM result** is saved (atomically), before any validation.
- `refresh=True`: skip the read, still write. A re-roll replaces the entry, so the next cached run returns the new result.
- `cache_dir=None`: no read, no write.

Consequences:
- `user` is the *rendered* prompt, so editing the script re-keys only the scenes whose text changed, and editing a `.jinja` template or `STRUCTURE_SYSTEM` re-keys every call made with it.
- The model part of the key is the configured model *name* (`LNVOX_LLM_MODEL` / `settings.llm.model`), not the loaded weights. Running by hand without the launcher's exported `LNVOX_LLM_MODEL` uses a different name and misses the cache. Swapping the GGUF behind an unchanged name does not re-key.
- Validation and canonicalization run on every pass, cached or not, so changes to those rules take effect without new LLM calls.

### `structure_chunk`

```python
def structure_chunk(
    client: LLMClient,
    chunk: str,
    cache_dir: Path | None = None,
    *,
    refresh: bool = False,
) -> tuple[SceneStructure, bool]
```

Renders `scenario_structure.jinja` with the chunk and calls `cached_structured(schema=SceneStructure, desired=max(4096, int(len(chunk) * 1.2)), floor=4096)`. The prompt tells the model to split a dialogue line around an embedded italic stage direction into dialogue / staging / dialogue items, to strip trailing separators from speaker labels, to leave scene headers out, and to prefer `staging` when unsure between staging and cue.

### `extract_roster`

```python
def extract_roster(
    client: LLMClient,
    roster_chunks: list[str],
    cache_dir: Path | None = None,
    *,
    refresh: bool = False,
) -> tuple[list[RosterEntry], int]
```

One `scenario_roster.jinja` call per roster chunk (`schema=RosterList, desired=4096, floor=2048`). It produces one `RosterEntry(name, description, actor)` per **role**, even when several roles share a performer. It returns the concatenated entries and the number of cache hits.

### `extract_characters`

```python
def extract_characters(
    client: LLMClient,
    roster: list[RosterEntry],
    scenes: list[ScriptScene],
    cache_dir: Path | None = None,
    *,
    refresh: bool = False,
) -> CharacterList
```

Builds the casting list:

1. Collect every **dialogue** speaker in order of first appearance, skipping group labels. Up to two sample lines per speaker are kept, each truncated to 160 characters.
2. Attach each speaker's roster `description` as `bio`, matched by [`_label_stem`](#_label_stem).
3. One `scenario_characters.jinja` call (`schema=CharacterList, desired=max(4096, 200 * n + 1024), floor=4096`) fills `gender`, `approx_age` and a voice-oriented `description` in the script's language.
4. The **names are ours**: the result is re-ordered to the speaker list, and LLM entries are matched by exact name. A speaker the LLM missed or renamed gets `Character(name=…, description=<roster bio or "Speaking role.">)` with gender and age `unknown`.

Roster roles that never speak are not in `01_characters.json`. If there are no speakers, no LLM call is made and the list is empty.

## Notes

- The script is structured in a single process with no per-scene artifacts. Resumability comes entirely from the cache.
- `--no-cache` on the CLI passes `refresh_cache=True`: every call is re-rolled and its fresh result **overwrites** the cache entry, so the next cached run reuses the re-roll.
- Without `--scenario-file`, the launcher skips this step only when both `00_script.json` and `01_characters.json` exist under `$LNVOX_ARTIFACTS_DIR/<id>/` (default `artifacts/`), and refuses to start (exit 2) when either is missing. `00_script.json` is written last and atomically, after `00_text.jsonl` and `01_characters.json`, so with the current code its presence alone already means the whole ingest finished; the second file guards against folders from older runs.
