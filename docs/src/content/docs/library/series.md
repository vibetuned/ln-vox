---
title: lnvox.series
description: Hierarchical book ids and prior-volume detection — series_root, find_prior_volumes (exact matching and ordering rule), latest_prior_volume, book_path.
sidebar:
  order: 3
  label: lnvox.series
---

`lnvox.series` (`src/lnvox/series.py`) turns a hierarchical book id such as `toaru/volume-02` into filesystem paths, and finds the **prior volumes** of the same series under `artifacts/`. Those let a later volume keep an earlier volume's voices. It has no dependencies and does no I/O beyond listing one directory. For the workflow, see the [Series guide](/guides/series/).

## At a glance

| Function | Returns | Used by |
| --- | --- | --- |
| [`book_path`](#book_path) | `artifacts_dir / book_id` | nothing in the repo (the CLI uses its own `_book_dir`) |
| [`series_root`](#series_root) | parent dir of a nested id, or `None` | `find_prior_volumes` |
| [`find_prior_volumes`](#find_prior_volumes) | earlier-numbered sibling volumes with the same prefix, oldest → newest | `lnvox s1`, `lnvox voice cast`, `scripts/voicebank_studio.py` |
| [`latest_prior_volume`](#latest_prior_volume) | the last of those, or `None` | nothing in the repo |

## Book ids

A book id is either **nested**, `<series>/<volume>` (e.g. `toaru/volume-02`), or **flat** (e.g. `standalone-book`). Artifacts always go to `artifacts/<book_id>/` with the slashes kept, so nested ids produce a series directory holding one sub-directory per volume:

```text
artifacts/
  toaru/                 <- series_root("toaru/volume-02")
    volume-01/           <- prior volume
      01_characters.json
      04_voice_assignments.json
    volume-02/           <- current book
```

Deeper nesting works: the series is everything before the **last** `/`, so `a/b/volume-03` has series root `artifacts/a/b/`. Flat ids have no series and never find prior volumes.

## API

### `book_path`

```python
def book_path(artifacts_dir: Path, book_id: str) -> Path
```

Returns `artifacts_dir / book_id`. The CLI resolves the same path through `cli._book_dir()`, which uses [`Settings().artifacts_dir`](/library/config/#settings).

### `series_root`

```python
def series_root(artifacts_dir: Path, book_id: str) -> Path | None
```

For an id containing `/`, returns `artifacts_dir / <everything before the last "/">`. Returns `None` for a flat id. The directory isn't checked for existence.

### `find_prior_volumes`

```python
def find_prior_volumes(artifacts_dir: Path, book_id: str) -> list[Path]
```

Returns the directories the pipeline treats as earlier volumes of the same series, oldest → newest.

**Rule:**

1. If `series_root()` is `None` (flat id) or that directory doesn't exist, return `[]`.
2. List the directories directly under the series root. Plain files and hidden directories (names starting with `.`) are skipped.
3. Split the current leaf (`book_id.rsplit("/", 1)[-1]`) into a **prefix** and a **trailing number**: `volume-02` → (`volume-`, 2), `book3` → (`book`, 3).
4. A sibling is a prior volume when its name also ends in a number, has the **same prefix** (compared case-insensitively) and a **strictly smaller number**.
5. Sort the matches **numerically** (sibling name breaks ties).

What that means in practice:

- **Padding doesn't matter.** `volume-9` comes before `volume-10`, and `volume-2` sits between `volume-01` and `volume-03`. A sibling with the same number in another spelling (`volume-2` next to `volume-02`) is not a prior volume.
- **Stray directories are ignored.** For `volume-02`, siblings such as `extras`, `covers`, `_shared`, `.cache`, `vol-01` or `volume-01-notes` never count. `Volume-01` does count (same prefix, ignoring case), and `Volume-07` doesn't (it is a later volume).
- **Leaves without a trailing number** (e.g. `toaru/gamma`) fall back to name ordering: every non-hidden sibling directory whose name sorts before the leaf (Python `str` comparison), sorted by name.

The current volume is excluded (the comparison is strict). The current book's directory doesn't need to exist, and nothing inside the siblings is read. Every caller then checks each candidate for `01_characters.json` / `04_voice_assignments.json` and skips it when they're missing.

The order matters: voice cast folds the priors in sequence and the newest volume wins.

Example:

```python
from pathlib import Path
from lnvox.series import find_prior_volumes

find_prior_volumes(Path("artifacts"), "toaru/volume-03")
# [PosixPath('artifacts/toaru/volume-01'), PosixPath('artifacts/toaru/volume-02')]
find_prior_volumes(Path("artifacts"), "standalone-book")
# []
```

### `latest_prior_volume`

```python
def latest_prior_volume(artifacts_dir: Path, book_id: str) -> Path | None
```

Returns `find_prior_volumes(...)[-1]` (the highest-numbered earlier volume), or `None` when there are none. Nothing in the pipeline calls it at the moment. Voice cast deliberately folds **every** prior volume rather than only the latest.

## Who uses it

- **`lnvox s1`** ([stage](/stages/s1-characters/), [library](/library/stages/s1-characters/)). It prints the priors found and passes them to `s1_characters.run(prior_volume_dirs=…)`, which loads each prior's `01_characters.json`. Those casts are **not** sent to the merge LLM. `merge_clusters` accepts `prior_volume_casts` only for compatibility and ignores it. Their only effect is that the cluster/merge path (and `01_characters_merge_log.json`) runs even for a single-chapter book. Cross-volume continuity is handled at voice-cast time, not in s1.
- **`lnvox voice cast`** ([stage](/stages/voice-cast/), [matcher](/library/voices/matcher/)). For each prior volume holding `04_voice_assignments.json`, it loads that file plus the prior's `01_characters.json` (if present) into one `PriorCastIndex`, keyed by names and aliases, newest volume wins. Recurring characters (including the Narrator) then keep their clip. This volume's own existing assignments are matched separately: an entry marked `"locked": true` beats the prior volumes (and `--narrator-clip` and `--fresh`), while an unlocked entry is the weakest layer, kept only for characters no prior volume knows and dropped by `--fresh`. See [voice cast](/stages/voice-cast/#cross-volume-reuse).
- **`scripts/voicebank_studio.py`** ([guide](/guides/voicebank-studio/)) builds one index per prior volume so the Casting tab can show which volume a voice was inherited from. It derives the book id from the casting file's path relative to `--artifacts`. A casting file outside that directory gets no series context.

## Notes

- DESIGN.md's overview says s1 "merges per-chapter character lists with the previous volume's cast". The code (and the later cross-volume continuity section of DESIGN.md) doesn't feed prior casts into the s1 merge.
- The functions take `artifacts_dir` explicitly. The CLI passes `Settings().artifacts_dir` (`LNVOX_ARTIFACTS_DIR`), and the Studio its `--artifacts` directory (same default). The launcher calls no series code itself: its stages resolve prior volumes under the artifacts root they share with it.
