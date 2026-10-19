---
title: lnvox.voices.matcher
description: LLM-driven character-to-clip casting - cast_book, cross-volume PriorCastIndex, target inference, filters, ranking and Narrator handling.
sidebar:
  order: 4
  label: matcher
---

`src/lnvox/voices/matcher.py` assigns every character in `01_characters.json`, plus the Narrator, a reference clip from the voicebank. It is the library behind Stage V ([`lnvox voice cast`](/stages/voice-cast/)), which builds the cross-volume index, calls [`cast_book`](#cast_book) and writes `04_voice_assignments.json`. Each character is cast on its own, from demographics and text; the matcher never compares two castings. That is the [casting audit](/library/voices/audit/)'s job.

## At a glance

| Name | Purpose |
|---|---|
| [`cast_book`](#cast_book) | Cast a whole book; returns a [`BookCasting`](/library/voices/schema/#bookcasting). |
| [`PriorCastIndex`](#priorcastindex) | Cross-volume memory: identity keys → most recent prior casting. |
| [`infer_target`](#infer_target) | LLM call 1: character → [`VoiceTarget`](/library/voices/schema/#voicetarget). |
| [`rank_candidates`](#rank_candidates) | LLM call 2: filtered clips → ranked top-N. |
| [`_hard_filter`](#_hard_filter), [`_soft_filter_by_accent`](#_soft_filter_by_accent) | Candidate filtering. |
| [`_offered_choices`](#_offered_choices), [`_fallback_choice`](#_fallback_choice) | Keep only ranked ids that were offered; deterministic pick when none is. |
| [`descriptor_from_clip`](#descriptor_from_clip) | Dramabox descriptor built from clip metadata alone. |
| `DEFAULT_NARRATOR_GENDER` / `_AGE` / `_ACCENT` | `"female"`, `"adult"`, `"england"`. |
| `SYSTEM` | Shared system prompt: "You match audiobook characters to voice-bank reference clips…", raw JSON only. |

## API

### Narrator defaults

```python
DEFAULT_NARRATOR_GENDER = "female"
DEFAULT_NARRATOR_AGE = "adult"
DEFAULT_NARRATOR_ACCENT = "england"
```

The fixed target used to auto-cast the Narrator when there is no `--narrator-clip` and no usable prior-volume or existing Narrator. The accent is a preference (england clips are ranked first), not a filter. Lecture mode's Narrator auto-cast uses them too (DESIGN.md §13.4).

### `descriptor_from_clip`

```python
def descriptor_from_clip(
    clip: VoiceClip, role_tail: str = "warm clear voice, measured and engaging storyteller"
) -> str
```

Composes a Dramabox descriptor from the clip's metadata: `"<age band with spaces>, [<accent adjective>, ]<male|female>, <role_tail>"`. The adjective comes from `_ACCENT_ADJ` (`us` → American, `england` → British, `scotland` → Scottish, `wales` → Welsh, `ireland` → Irish, `australia` → Australian, `canada` → Canadian, `indian` → Indian, `newzealand` → New Zealand, `african` → African, `philippines` → Filipino, `hongkong` → Hong Kong, `singapore` → Singaporean, `malaysia` → Malaysian, `bermuda` → West Indian). `any`, `other` and anything else get no adjective.

```python
descriptor_from_clip(clip)  # clip: female / adult / england
# 'adult, British, female, warm clear voice, measured and engaging storyteller'
```

Used for every Narrator casting, so Stage 3 does not write a free-text Narrator descriptor that contradicts the reference clip. The Voicebank Studio imports it as well.

### `_derive_narrator_demographics`

```python
def _derive_narrator_demographics(voice_descriptor: str) -> tuple[str, str]
```

Regex heuristic for `(gender, age)` from a descriptor such as `"mid-thirties male, smooth baritone"`. Gender is `female` if any of `female|woman|girl|soprano|alto|mezzo` appears, else `male`. Age is the first match in the order `teen` → `child` → `elder` (`elder|elderly|old|sixties…nineties`) → `young_adult` (`young adult|twenties|thirties|mid-20s|mid-30s|…`) → `adult` (`adult|middle-aged|forties|fifties`), else `adult`. Only used when synthesising the Narrator from a legacy `03_voice_profiles.json` (see step 0 of [cast_book](#cast_book)).

### `PriorCastIndex`

```python
class PriorCastIndex:
    def __init__(self) -> None
    def add_volume(self, casting: BookCasting, cast: Iterable[Character] = ()) -> None
    def lookup(self, name: str, aliases: Iterable[str] = (), gender: str = "", shared_words: Iterable[str] = ()) -> CharacterCasting | None
    def __len__(self) -> int
```

Cross-volume casting memory. Stage 1's canonical names drift between volumes ("Aldric Vane" / "Vane Aldric" / "Aldric"), and series casts rotate so characters skip volumes. Matching exact names against only the latest volume loses the voice in both cases, so the index folds in **every** prior volume and keys each casting by every name the character was known by. Internally it is `_by_key: dict[str, tuple[CharacterCasting, bool]]`, mapping a key to the casting and whether the key came from that entry's canonical name.

#### Identity keys (`_keys`)

```python
@staticmethod
def _keys(name: str, aliases: Iterable[str] = ()) -> list[tuple[str, bool]]
```

For the name and then each alias, in that order:

1. Normalise: lowercase, strip, collapse internal whitespace (`_norm_name`). Empty strings are skipped.
2. Emit the normalised form **and** its token-sorted form (`"vane aldric"` → `"aldric vane"`), skipping duplicates.
3. Flag each key `True` if it came from the canonical name, `False` if from an alias.

The result is in priority order: canonical keys first, then alias keys in alias order.

#### `add_volume`

Folds one volume's assignments in. Call it oldest → newest; later calls overwrite earlier ones **per key** (`dict.update`), so the newest volume wins.

- `cast` is that volume's `01_characters.json` character list. It is the only source of aliases, since a casting stores just `character_name`. Aliases are looked up by exact `character_name`.
- Entries with an empty `assigned_clip_id` are skipped, so an unresolved casting in a newer volume never shadows an older real assignment.
- A key claimed by two **different** characters in the same volume (two people both aliased "the Captain") identifies neither and is dropped from that volume's contribution. Whatever an older volume had stored under that key stays.

#### `lookup`

Walks the current character's keys in priority order and returns the first acceptable hit:

- **Canonical on both sides** (the current key comes from the current name and the stored key came from the prior entry's name): trusted as-is, regardless of gender.
- **Anything alias-mediated** (either side): skipped when the key is one of `shared_words`; otherwise accepted only if `gender` is `"male"` or `"female"` and equals the prior entry's `target.gender`. When a key is rejected, the walk continues with the next one.

Returns `None` when no key is acceptable. The gender guard exists because Stage 1 sometimes lists a companion's name among a character's aliases; a cross-gender match through such an alias is how one character steals another's voice. The `shared_words` guard covers the same-gender case: Stage 1 also lists a bare family name as an alias ("Vane" for Aldric Vane). A later character called Vane, cast alongside Aldric Vane, would otherwise inherit his voice through that alias. `cast_book` passes each character's [`shared_name_words`](#shared_name_words).

| Prior volume | Current volume | Outcome |
|---|---|---|
| `Sera Kalt` (female) | `Kalt Sera`, gender female | hit: canonical token-sorted key on both sides |
| `Aldric Vane`, alias `Aldric` | `Aldric`, gender male | hit via alias key, genders agree |
| `Aldric Vane`, alias `Aldric` | `Aldric`, gender `unknown` | no hit: alias-mediated and gender unknown |
| `Aldric` (male), alias `Vane` | `Sera`, alias `Vane`, gender female | no hit: alias key, gender disagrees |
| `Aldric` (male) | `Aldric`, gender female | hit: canonical on both sides beats gender |
| two characters aliased `the Captain` | `the Captain` | no hit: ambiguous key dropped |
| `Aldric` (male), alias `Vane` | `Vane` (male), cast next to `Aldric`, alias `Aldric Vane` | no hit: `vane` names two current characters |

`len(index)` is the number of keys (the CLI prints it as "N identity keys").

### `shared_name_words`

```python
def shared_name_words(cast: Iterable[Character]) -> dict[str, set[str]]
```

Maps each character's name to the single words (normalised like `_keys`) of its name and aliases that also appear in another character's name or aliases in `cast`. For example, a family name or a given name two characters share. `lookup` refuses an alias-mediated match on such a word alone.

[`cast_book`](#cast_book) also builds a second, private index from the volume's own `existing` casting, so an entry is found by the same name, alias and token-order rules after an s1 re-run renames a character.

### `infer_target`

```python
def infer_target(
    client: LLMClient,
    character: Character,
    voice_descriptor: str = "",
    profile_accent: str = "any",
) -> VoiceTarget
```

LLM call 1. Renders `voice_target.jinja` with the character's `name`, `gender`, `approx_age`, `description`, plus `voice_descriptor` and `profile_accent` (both from `03_voice_profiles.json` when it exists, else `""` / `"any"`), and asks for a `VoiceTarget` via `client.structured(system=SYSTEM, user=…, schema=VoiceTarget, max_tokens=1024)`. The prompt tells the model to map into the voicebank's vocabulary (`male|female`, four age bands), to copy `profile_accent` into `accent_keywords` (or `["any"]`), and to keep keyword lists to 1–4 items. See [LLMClient](/library/llm/client/) and [prompts](/library/llm/prompts/).

In the normal launcher order (voice cast before s3) there is no profiles file on a first run, so the accent preference is `any`.

### `_hard_filter`

```python
def _hard_filter(voicebank: Voicebank, target: VoiceTarget) -> list[VoiceClip]
```

Non-negotiable filter: clips whose `gender` **and** `age_band` both equal the target's. Order is manifest order. There is no relaxation for regular characters: an empty bucket means no candidates.

### `_soft_filter_by_accent`

```python
def _soft_filter_by_accent(
    candidates: list[VoiceClip], target: VoiceTarget, max_keep: int = 80
) -> list[VoiceClip]
```

If `target.accent_keywords` is empty or contains `"any"`, returns the first `max_keep` candidates unchanged. Otherwise moves clips whose `accent` (lowercased) is in the lowercased keyword set to the front, keeps the others after them, and truncates to `max_keep`. Both sides are Common Voice accent codes, so matching is exact; there are no aliases or substring rules. `cast_book` passes `max_keep=max_candidates_per_char` (60).

:::note
Truncation keeps the first N in manifest order. In a large bank (say 170 `male/young_adult` clips) the ranking LLM only ever sees the first 60 of a bucket, or the first 60 after accent matches. Which clips those are depends on how the manifest was built (see [merging](/stages/voicebank/#merging-with-an-existing-bank)).
:::

### `_offered_choices`

```python
def _offered_choices(ranked: MatchResult, candidates: list[VoiceClip]) -> list[_RankedChoice]
```

The ranked choices whose `clip_id` is one of `candidates`, in rank order. The ranking LLM only ever sees those ids, so anything else was invented (or copied from the prompt's example). Such an id used to be committed and fail only at s4, which refuses a casting that names clips missing from the bank. A real bank id that was not offered (a clip of the wrong gender, say) is dropped too.

### `_fallback_choice`

```python
def _fallback_choice(ranked: MatchResult, candidates: list[VoiceClip]) -> tuple[_RankedChoice, str]
```

Used when `_offered_choices` leaves nothing (the model returned no choice, or only ids it was not shown). Returns a `_RankedChoice` for `candidates[0]`, the first clip in filter order (accent matches first, then manifest order), with `score=0.0` and `reason="Fallback: <why>; first candidate in filter order."`, plus the note `cast_book` stores in `audit_note`: `voice cast: none of the N ranked clip id(s) was among the M offered; fell back to the first candidate` (or `ranking returned no choice`).

### `rank_candidates`

```python
def rank_candidates(
    client: LLMClient,
    character: Character,
    target: VoiceTarget,
    candidates: list[VoiceClip],
    top_n: int = 3,
) -> MatchResult
```

LLM call 2. Returns an empty `MatchResult` without calling the LLM if `candidates` is empty. Otherwise renders `voice_match.jinja` with:

- the character: `name`, `gender`, `approx_age`, `description` (no aliases or evidence);
- the target (`VoiceTarget.model_dump()`);
- one entry per candidate: `id`, `accent`, `sample_sentence` (the clip's first sample sentence, cut to 160 characters), `duration`;
- `candidate_count` and `top_n`.

The prompt asks for the `top_n` best matches on accent, timbre and delivery, preferring closer accent, sample sentences whose register fits the character, and a diverse list. Output budget is `max(1024, 200 * top_n + 512)` tokens (1112 for the default `top_n=3`).

`rank_candidates` returns the model's list as-is; [`cast_book`](#cast_book) filters it through [`_offered_choices`](#_offered_choices) before committing anything.

The LLM never hears audio. It ranks on accent codes, transcripts and durations, so `timbre_keywords` and `manner_keywords` can only be matched against what a transcript suggests. Actual acoustic separation is checked later by the [audit](/library/voices/audit/).

### `cast_book`

```python
def cast_book(
    client: LLMClient,
    book_id: str,
    cast: Iterable[Character],
    voicebank: Voicebank,
    *,
    profiles: VoiceProfileList | None = None,
    top_n: int = 3,
    max_candidates_per_char: int = 60,
    prior_index: PriorCastIndex | None = None,
    existing: BookCasting | None = None,
    narrator_clip_override: str | None = None,
    on_character_done=None,
) -> BookCasting
```

Casts every character in `cast`, plus a synthesised Narrator if the cast has none, and returns a `BookCasting` in cast order (a synthesised Narrator is last). It writes nothing; the CLI saves the result.

**Parameters**

- `client` — an [LLMClient](/library/llm/client/); only `render` and `structured` are used, so tests pass a stub.
- `book_id` — copied into `BookCasting.book_id`.
- `cast` — `01_characters.json` characters.
- `voicebank` — the loaded manifest; all clip ids are resolved against it.
- `profiles` — `03_voice_profiles.json` if present; supplies `voice_descriptor` / `accent` to target inference and a legacy Narrator description.
- `top_n` — ranked choices requested per character, default `3`.
- `max_candidates_per_char` — cap on clips shown to the ranking call, default `60`. Not exposed on the CLI.
- `prior_index` — cross-volume memory built from prior volumes only; `None` disables it.
- `existing` — this volume's own current casting (its `04_voice_assignments.json`; the CLI passes only the locked entries under `--fresh`). Locked entries win over everything; unlocked ones are the weakest reuse layer. See the decision order below.
- `narrator_clip_override` — clip id to force for the Narrator; beats auto-cast, prior reuse and an unlocked own entry, but not a locked one.
- `on_character_done` — optional `callback(character, casting)` after each character (the CLI prints progress with it).

**Raises** `ValueError("--narrator-clip '<id>' not in voicebank.")` when the override id is not in the bank. This is checked first, before any character is cast, because a synthesised Narrator comes last and a late check would waste every other character's LLM calls. (`lnvox voice cast` checks the same thing even earlier and exits `2`.) LLM errors from `structured` propagate.

#### Step 0: Narrator synthesis

If no character is named exactly `Narrator`, one is appended with `aliases=[]`, `evidence=[]` and demographics from the first source that applies (the same precedence as the decision order below):

1. a locked `Narrator` entry in `existing` whose clip is in the bank → its `target.gender` / `target.age_band`;
2. `narrator_clip_override` → that clip's gender and age band;
3. the prior index's `Narrator` entry, else an unlocked one in `existing` → its `target.gender` / `target.age_band`;
4. a `Narrator` profile in `profiles` → `_derive_narrator_demographics(descriptor)`, and the descriptor becomes the description;
5. otherwise `DEFAULT_NARRATOR_GENDER` / `DEFAULT_NARRATOR_AGE` (`female` / `adult`) with description `"Neutral audiobook narrator for a modern action/fantasy young-adult novel."`.

These demographics only feed the gender checks of steps 3 and 4 and the character payload of a ranking call. The auto-cast target in step 6 below is always the fixed default.

#### Per-character decision order

For each character, the first branch that applies wins. "Own entry" is the character's entry in `existing`, looked up by name / alias like a prior.

1. **Locked own entry.** The own entry has `locked: true` and its clip is in the bank: copied whole (`target`, `ranked`, `locked`, `audit_note`, …) under the current name, with no gender check, since a lock is a deliberate hand edit. A Narrator with an empty `voice_descriptor` gets `descriptor_from_clip(clip)`. No LLM call. A locked entry whose clip is missing from the bank is ignored, and whatever is cast instead gets `audit_note = "voice cast: locked clip <id> is not in the voicebank; re-cast"`.
2. **Narrator override.** `character.name == "Narrator"` and an override is set. Casting uses the clip directly: `target` = clip's gender, age band and `[accent]`; `candidates_considered=1`; `ranked=[{clip_id, score: 1.0, reason: "Manual --narrator-clip override."}]`; `voice_descriptor=descriptor_from_clip(clip)`. No LLM call.
3. **Prior reuse.** `prior_index.lookup(name, aliases, gender, shared_words)` returns an entry whose clip is in the current bank, and the clip's gender matches at least one of the known genders (the current character's, the prior target's). A clip that matches neither, such as a hand-edit that pointed a female character at a male clip, is recast rather than propagated through the series. The prior `target`, `candidates_considered`, `ranked` and `assigned_clip_id` are copied; `voice_descriptor` is the prior's, or for the Narrator `descriptor_from_clip(clip)` when the prior had none. No LLM call.
4. **Unlocked own entry.** Same bank and gender checks as step 3; copied whole like step 1. This is the weakest reuse layer: it only applies when no prior volume matched, so continuity chains win but a first-appearance character is not re-rolled on a re-run. No LLM call.
5. **Gender outside the bank.** `character.gender` is not `male`/`female` (`nonbinary`, `unknown`). Written with a placeholder `male / adult` target, no candidates and `assigned_clip_id=""`. s4 renders such characters descriptor-only. No LLM call.
6. **Narrator auto-cast.** No target inference (inferring from the synthetic description tends to drift). Target is `female / adult / [england]`, where the accent is only an ordering preference: the hard filter never looks at it. Candidates come from a relaxation ladder: female + adult, then any female clip, then every clip. The soft filter puts `england` clips first and caps at 60. One candidate → assigned without the LLM (`reason: "Sole candidate (<rung>)."`, rung `female/adult, england first`, `relaxed age (female/any age)` or `any clip`); more → one `rank_candidates` call, keeping the [offered](#_offered_choices) choices and taking the first, else the [fallback](#_fallback_choice). `voice_descriptor = descriptor_from_clip(assigned)`.
7. **Everyone else.** `infer_target` (LLM call 1) → `_hard_filter` → `_soft_filter_by_accent(max_keep=60)` → `rank_candidates` (LLM call 2, skipped when there are no candidates) → [`_offered_choices`](#_offered_choices). `ranked` holds only offered ids and `assigned_clip_id` is the first of them. If none is left, the [fallback](#_fallback_choice) assigns the first candidate and records it in `ranked` and `audit_note`. `assigned_clip_id` is `""` only when there were no candidates. `voice_descriptor` stays empty; Stage 3 writes these with full personality.

**Provenance.** When step 2 or 3 assigns the clip the own entry already had, that entry's `audit_note` and non-empty `voice_descriptor` are kept; a different clip drops them, so a descriptor written for one voice never follows the character onto another.

So a fresh character costs two LLM calls, the auto-cast Narrator at most one, and reused, overridden or skipped characters none.

:::note[No relaxation outside the Narrator path]
An empty `(gender, age_band)` bucket leaves a regular character unassigned (`no match (0 candidate(s))`). Seed more speakers or cast by hand.
:::

```python
from lnvox.llm.client import LLMClient
from lnvox.llm.schemas import CharacterList
from lnvox.voices import manifest
from lnvox.voices.matcher import PriorCastIndex, cast_book
from lnvox.voices.schema import BookCasting
from pathlib import Path

book = Path("artifacts/myseries/volume-02")
cast = CharacterList.model_validate_json((book / "01_characters.json").read_text()).characters
prior = PriorCastIndex()
v1 = Path("artifacts/myseries/volume-01")
prior.add_volume(
    BookCasting.model_validate_json((v1 / "04_voice_assignments.json").read_text()),
    CharacterList.model_validate_json((v1 / "01_characters.json").read_text()).characters,
)
own = book / "04_voice_assignments.json"
existing = BookCasting.model_validate_json(own.read_text()) if own.exists() else None
result = cast_book(LLMClient(), "myseries/volume-02", cast,
                   manifest.load(Path("voicebank")), prior_index=prior, existing=existing)
```

## Notes

- **Determinism.** Given the same prior index, `existing` casting and bank, reuse is deterministic; fresh casts depend on the LLM. The `existing` layer (see [Stage V](/stages/voice-cast/#cross-volume-reuse)) is what makes re-running `voice cast` idempotent.
- **Switching banks.** A prior clip id that is not in the current bank is ignored and the character is recast, so pointing `LNVOX_VOICEBANK` at a different bank quietly recasts the series rather than failing.
- **Dead branch.** The general path (step 7) still contains code that derives a descriptor for a character named `Narrator`, but the Narrator always takes one of branches 1 to 6 first.
- DESIGN.md §2.4 / §6.3 describe the Narrator as synthesised from the s3 descriptor, else `male/adult`, and "cast like any other character". The code uses a fixed `female / adult` target (england preferred) with no target inference.
- DESIGN.md §2.4 / §6.4 describe reuse from only the most recent prior volume by exact canonical name; the code folds every prior volume with alias and token-order keys, as described above.
