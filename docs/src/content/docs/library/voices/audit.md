---
title: lnvox.voices.audit
description: Casting audit - scene co-occurrence, pitch-collision detection between scene partners, and constrained automatic repair.
sidebar:
  order: 6
  label: audit
---

`src/lnvox/voices/audit.py` answers one question the matcher never asks: do characters who talk to each other sound different? It weights every pair of speakers by how much dialogue they share, flags pairs whose assigned clips are the same or too close in pitch, and can reassign the quieter side of each pair. [`lnvox voice audit`](/stages/voice-audit/) is the only caller; it sits between Stage V and Stage 3, where a casting change is still free (DESIGN.md §20).

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| `NARRATOR_NAME` | `"Narrator"` | Speaker of every narration beat; never moved by repair |
| `MIN_SEMITONES` | `2.5` | Pairs closer than this in median f0 collide |
| `MIN_SHARED` | `3` | Pairs with less shared-scene weight are ignored |
| `_MIN_BANDWIDTH`, `_MIN_SNR` | `10000.0`, `38.0` | Quality floor for a replacement clip |
| `_RELAXED_BANDWIDTH`, `_RELAXED_SNR` | `9000.0`, `32.0` | Last-resort floor |
| [`scene_cooccurrence`](#scene_cooccurrence) | function | Beats per speaker and shared weight per pair |
| [`find_collisions`](#find_collisions) | function | Scene-sharing pairs whose voices are not separable |
| [`repair_collisions`](#repair_collisions) | function | Reassign the lower-speaking side of each collision |
| [`Collision`](#collision), [`Repair`](#repair) | frozen dataclasses | Results |

## The metric

Two characters are a problem when they **share scenes** and their voices are **not separable**.

- **Co-occurrence weight.** For every scene, count each speaker's beats; for each pair of speakers present, add `min(beats_a, beats_b)`. Summed over the book, this measures how much they actually converse. Merely appearing in the same book, or one line each in a crowded scene, barely registers.
- **Separation.** `semitones_apart(pitch_a, pitch_b)`, the absolute distance between the two assigned clips' median f0 from the [acoustic index](/library/voices/acoustics/). The same clip is separation zero.

A pair **collides** when weight `>= min_shared` (default 3) and either both sides have the same `assigned_clip_id` (kind `same clip`), or both pitches are known and the gap is strictly less than `min_semitones` (default 2.5; kind `too close`).

Pitch is a crude proxy for "sounds different": it ignores timbre and delivery. But it is cheap and reliably catches the failure that matters in practice, two same-gender, same-age-band clips within a semitone of each other.

## API

### `Collision`

```python
@dataclass(frozen=True)
class Collision:
    a: str
    b: str
    shared: int
    semitones: float
    same_clip: bool

    @property
    def kind(self) -> str  # "same clip" | "too close"
```

`a` and `b` are character names, in alphabetical order. `shared` is the pair's co-occurrence weight; `semitones` is the gap at detection time (`0.0` for the same clip).

### `Repair`

```python
@dataclass(frozen=True)
class Repair:
    character: str
    old_clip_id: str
    new_clip_id: str
    new_pitch: float
```

One applied reassignment; `new_pitch` is the replacement's median f0 in Hz.

### `scene_cooccurrence`

```python
def scene_cooccurrence(scene_dir: Path) -> tuple[Counter, Counter]
```

Returns `(beats_per_speaker, shared_scene_weight_per_pair)`.

**Reads** every `*.json` directly in `scene_dir` (sorted, not recursive), expecting the `02_scenes` / `03_directed` shape `{"scenes": [{"beats": [{"type": …, "speaker": …}, …]}, …]}`. Files that fail to parse are skipped silently. A beat with `type: "narration"` counts as `NARRATOR_NAME`'s whatever its `speaker`, since that is who Stage 3 gives it to. Any other beat with a falsy `speaker` (an unattributed line in `02_scenes/`) is ignored.

- `beats[name]` — the speaker's total beats across the book. The repair uses it to decide who moves.
- `pairs[(a, b)]` with `a < b` — the sum over scenes of `min(beats_a, beats_b)`.

:::note[Where the Narrator comes from]
In `02_scenes/` narration beats have `speaker: null`; in `03_directed/` Stage 3 sets `speaker: "Narrator"` on them. Counting every narration beat as the Narrator's makes the two sources agree, so the Narrator pairs with nearly every character on a first launcher run too, when Stage A can only read `02_scenes/`. Narrator collisions (DESIGN.md §20.1's Narrator / The Sphere case) are therefore caught before s3 rather than after it. The Narrator's own beat count is higher from `02_scenes/`, because s3 fuses consecutive narration beats; pair weights, being the lesser side's count, barely move.
:::

### `find_collisions`

```python
def find_collisions(
    casting: BookCasting,
    acoustics: dict[str, dict],
    pairs: Counter,
    *,
    min_semitones: float = MIN_SEMITONES,
    min_shared: int = MIN_SHARED,
) -> list[Collision]
```

Applies the metric above to every pair in `pairs`. A pair is skipped when its weight is below `min_shared`, when either side has no casting entry or an empty `assigned_clip_id`, or (different clips) when either clip has no measured pitch. The last case is left alone rather than guessed at. Speaker names are matched to `CharacterCasting.character_name` exactly.

**Returns** collisions sorted by `(-shared, semitones)`: the pairs that converse most first, and within equal weight the closest first. This is also the order repair works in.

### `repair_collisions`

```python
def repair_collisions(
    casting: BookCasting,
    voicebank: Voicebank,
    acoustics: dict[str, dict],
    beats: Counter,
    pairs: Counter,
    collisions: list[Collision],
    *,
    min_semitones: float = MIN_SEMITONES,
    min_shared: int = MIN_SHARED,
) -> tuple[list[Repair], list[Collision]]
```

Tries to fix each collision by moving one side to a new clip. **Mutates `casting` in place** for successful repairs and returns `(applied, unfixed)`. The caller decides whether to save (the CLI rewrites `04_voice_assignments.json` only if `applied` is non-empty).

**Setup**

- `used` — every clip currently assigned anywhere in the book. A replacement is always a clip nobody else in the book holds.
- `partners[name]` — everyone the character shares at least `min_shared` weight with. This is the same threshold as detection; requiring separation from *every* co-speaker would empty the pool in books where the Narrator pairs with 30+ speakers.
- **Protected**: the character named `Narrator`, and any casting with `locked: true`.

**Per collision, in the order given**

1. **Re-check.** Compute the pair's gap *as currently assigned*. If it is now `>= min_semitones`, or can't be measured, skip: an earlier repair already resolved it.
2. **Choose who moves.** Sort the two sides by `beats` ascending (ties keep alphabetical order) and drop protected sides. Take the first side not yet moved in this run; if both have moved, take the lower-speaking one anyway. The dominant voice is the one the listener has already learned, and the quieter character is cheaper to re-render. If both sides are protected, the collision is **unfixed**.
3. **Constraints.** The victim's current clip must exist in the bank (else unfixed). `avoid` is the measured pitch of every partner's current clip.
4. **Search** the bank for clips that are unused in the book, have the same `gender` as the victim's current clip, have a measured pitch, meet the quality floor, were never held by this character earlier in the run (this blocks A→B→A cycles), and are at least `min_semitones` from every pitch in `avoid`. Tiers, first non-empty wins:

   | Tier | Age bands | Bandwidth ≥ | SNR ≥ | Note appended |
   |---|---|---|---|---|
   | 1 | same as current clip | 10 000 Hz | 38 dB | none |
   | 2 | same or adjacent (`teen` ↔ `young_adult` ↔ `adult` ↔ `elder`) | 10 000 Hz | 38 dB | ` (adjacent age band)` |
   | 3 | same or adjacent | 9 000 Hz | 32 dB | ` (adjacent age band, relaxed quality)` |

   No candidate in any tier → **unfixed**. Nothing is moved to something worse than tier 3.
5. **Pick** the candidate with the highest `_quality` score (below); ties go to the earlier clip in manifest order.
6. **Apply.** Set `assigned_clip_id`, empty `voice_descriptor` (a stored descriptor describes the old clip, and Stage 3 would use it verbatim), write `audit_note = "voice audit: was {semitones:.1f} semitones from {other}, who shares its scenes{tier note}"` (the gap is the one recorded at detection), update `used`, record both clips in the character's history, and append a `Repair`.

```python
def _quality(metrics: dict) -> float:
    return (
        min(metrics.get("bandwidth", 0.0), 11000.0) / 1000.0 * 3.0
        + min(metrics.get("snr", 0.0), 50.0) * 0.6
        - min(metrics.get("hiss", 0.0), 1.0) * 15.0
    )
```

This is `quality_score` from [common_voice](/library/voices/common-voice/#quality_score) without the rumble and clipping terms.

A character may be moved more than once in a run (late collisions can involve two characters who have both moved already), but never back onto a clip it already held.

## What `locked` and the Narrator do

| Pair | Report | `--fix` |
|---|---|---|
| unprotected + unprotected | listed | lower-speaking side moves |
| unprotected + Narrator or locked | listed | the unprotected side moves, even if it speaks more |
| Narrator + locked, or locked + locked | listed | always unfixed |

Protection only affects repair. A deliberate share, such as a pseudonym that is really the narrator, is reported on every audit and keeps `voice audit` exiting non-zero in report mode. That is expected; the launcher ignores the exit code. A re-run of `lnvox voice cast` keeps locked entries exactly as they are, so a lock set by hand protects the casting from both the audit and the next Stage V ([Stage V](/stages/voice-cast/#cross-volume-reuse)). A move made by the audit is not locked: for a character who also appears in an earlier volume, the next Stage V puts that volume's clip back.

## Example

```python
from pathlib import Path
from lnvox.voices import manifest
from lnvox.voices.acoustics import load_acoustics
from lnvox.voices.audit import scene_cooccurrence, find_collisions, repair_collisions
from lnvox.voices.schema import BookCasting

book = Path("artifacts/myseries/volume-02")
casting = BookCasting.model_validate_json((book / "04_voice_assignments.json").read_text())
vb_dir = Path("voicebank")
bank = manifest.load(vb_dir)
acoustics = load_acoustics(vb_dir, bank.clips, progress=print)

beats, pairs = scene_cooccurrence(book / "02_scenes")
collisions = find_collisions(casting, acoustics, pairs)
for c in collisions:
    print(c.kind, c.a, c.b, c.shared, f"{c.semitones:.1f} st")

applied, unfixed = repair_collisions(casting, bank, acoustics, beats, pairs, collisions)
```

## Notes

- **Only the moved side changes.** Repair sets `assigned_clip_id` and `audit_note` and empties `voice_descriptor`; `target` and `ranked` keep describing the original pick. Re-run s3 afterwards so Stage 3's descriptors match the new clip. The launcher runs it next, and s3's casting-aware cache regenerates just the moved characters' profiles and re-voices their chapters; no `--regen-profiles` needed.
- DESIGN.md §20.4 says a replacement must match the original's age band and that widening the band "stays manual". The code widens to adjacent bands automatically (tiers 2 and 3) and records it in `audit_note`.
- Characters with no clip (skipped by the matcher) and clips missing from the acoustic index are invisible to the audit.
