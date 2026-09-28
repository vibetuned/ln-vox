---
title: lnvox.voices.manifest
description: Load, save and summarise the voicebank index at <voicebank>/manifest.json.
sidebar:
  order: 2
  label: manifest
---

`src/lnvox/voices/manifest.py` is the only read/write path for `<voicebank>/manifest.json`. Every `lnvox voice …` command, Stages 3 and 4, `lnvox scenario` and the Voicebank Studio go through it. It does no merging or validation beyond Pydantic parsing; callers decide what goes into the [`Voicebank`](/library/voices/schema/#voicebank) they save.

## At a glance

| Name | Purpose |
|---|---|
| `MANIFEST_FILENAME` | `"manifest.json"` |
| [`manifest_path`](#manifest_path) | `<voicebank_dir>/manifest.json` |
| [`load`](#load) | parse the manifest, or an empty bank if it doesn't exist |
| [`save`](#save) | write the whole manifest (creates the directory) |
| [`summarize`](#summarize) | counts for `lnvox voice list` |

The voicebank directory itself is chosen by the CLI helper `_voicebank_dir()` in `src/lnvox/cli.py`: `Path(os.environ.get("LNVOX_VOICEBANK", "voicebank"))`, relative to the working directory. See [Building the voicebank](/stages/voicebank/#per-language-banks).

## API

### `manifest_path`

```python
def manifest_path(voicebank_dir: Path) -> Path
```

Returns `voicebank_dir / "manifest.json"`. No filesystem access.

### `load`

```python
def load(voicebank_dir: Path) -> Voicebank
```

Parses the manifest with `Voicebank.model_validate_json`.

**Returns** the parsed `Voicebank`, or an empty `Voicebank()` (version `"1"`, no clips) when the file does not exist. An empty result is how callers detect "not seeded yet": `voice cast` and `s4` exit with an error telling you to run `lnvox voice seed-cv` first.

**Raises** `pydantic.ValidationError` when the file exists but is malformed (for example a clip with `gender: "nonbinary"` or an unknown `age_band`). It is not caught anywhere, so a hand-edited manifest with a typo fails every command loudly.

**Reads** `<voicebank_dir>/manifest.json`. It does not check that the `clip_path` WAVs exist.

### `save`

```python
def save(voicebank_dir: Path, voicebank: Voicebank) -> None
```

Creates `voicebank_dir` (with parents) if needed and overwrites the manifest with `voicebank.model_dump_json(indent=2)`.

**Writes** `<voicebank_dir>/manifest.json`, the whole file, non-atomically. It does not touch `clips/` or `acoustics.json`. Merging a new seed with an existing bank is done by the caller (`lnvox voice seed-cv`, see [Building the voicebank](/stages/voicebank/#merging-with-an-existing-bank)).

### `summarize`

```python
def summarize(voicebank: Voicebank) -> dict
```

Compact counts for `lnvox voice list`.

**Returns** a dict (example abridged):

```json
{
  "total": 410,
  "by_source": {"common_voice": 408, "manual": 2},
  "by_gender_age": {"male/young_adult": 171, "female/adult": 64, "female/elder": 9}
}
```

`by_gender_age` keys are `"<gender>/<age_band>"`, and a bucket only appears if it has at least one clip. These are the same buckets the matcher hard-filters on, so a missing or thin bucket here predicts poor casting (every elderly woman in the book drawing from nine clips, for example).

## Notes

- Two processes saving at once (for example the Studio and `seed-cv`) race: the last `save` wins and the other's clips drop out of the manifest, though their WAVs stay on disk.
- The manifest is the source of truth for clip ids. `acoustics.json` is a disposable cache keyed by the same ids ([lnvox.voices.acoustics](/library/voices/acoustics/)).
