---
title: Building the voicebank
description: Seed the reference-voice bank from Mozilla Common Voice with lnvox voice seed-cv, inspect it with voice list, and keep per-language banks.
sidebar:
  order: 15
  label: Voicebank
---

The voicebank is the pool of reference voices every casting draws from: a directory of mono 24 kHz WAVs plus `manifest.json` describing each clip's gender, age band and accent. You build it once per language, before the first `lnvox voice cast`. Every `04_voice_assignments.json` refers to clips by id, so the bank has to stay stable for as long as its castings are in use. Nothing in the launcher builds it for you.

## Command

### `lnvox voice seed-cv`

```bash
uv run lnvox voice seed-cv <CV_ROOT> [options]
```

| Option | Default | Meaning |
|---|---|---|
| `CV_ROOT` | required | The extracted Common Voice **locale** directory, the one containing `clips/` and `validated.tsv`. Typer rejects a missing path or a file. |
| `--max-speakers` | `150` | Maximum speakers (clips) to write in this run. |
| `--tsv` | `validated.tsv` | Preferred TSV inside `CV_ROOT`. Falls back to `validated.tsv`, `train.tsv`, `dev.tsv`, `test.tsv`, in that order. |
| `--target-seconds` | `12.0` | Stop concatenating a speaker's utterances once their trimmed speech reaches this. |
| `--min-seconds` | `8.0` | Drop speakers with less trimmed speech than this. |
| `--accent` | every accent | Keep only speakers with this accent code. Repeat for several (`--accent us --accent england`). Codes: `us`, `england`, `indian`, `canada`, `australia`, `scotland`, `african`, `newzealand`, `ireland`, `philippines`, `hongkong`, `singapore`, `malaysia`, `wales`, `bermuda`, `southatlandtic`, `other`, `any`. An unknown code is an error. |
| `--rank-by-quality` | off | Choose speakers by measured recording quality (bandwidth, SNR, hum, hiss, clipping) instead of `client_id` order, with per-gender/age-band quotas. Adds a probe pass. DESIGN.md §19. |
| `--max-candidates` | `max-speakers × 6` | With `--rank-by-quality`: how many speakers to probe. |

The output sample rate (24 000 Hz) is fixed on the CLI. Errors from the seeder (`RuntimeError`, `FileNotFoundError`, `ValueError`: missing voice extra, bad layout, unknown accent, accent filter matched nobody) print in red and exit `1`.

Recommended for an English bank (DESIGN.md §19.5):

```bash
uv run lnvox voice seed-cv data/<cv-corpus-NN.N-YYYY-MM-DD>/en/ \
    --max-speakers 400 --accent us --accent england --rank-by-quality
```

```text
Existing voicebank: 0 clip(s) in voicebank/
Reading metadata from en/validated.tsv
Scanned 1,234,567 rows; 98,765 eligible across 9,782 speakers (accent filter england/us dropped 45,678 row(s))
Probing recording quality of 2,400 candidate speaker(s)…
Selected 400 speaker(s) across 8 bucket(s); median bandwidth 10.4 kHz, median SNR 39 dB
  wrote 10/400 speaker clip(s)
…
Done. 400 speaker clip(s) written. Skipped 3 for insufficient duration, 0 for missing/unreadable MP3s.
✓ Voicebank now has 400 clip(s) (400 from this run)
```

(Numbers illustrative.)

### `lnvox voice list`

```bash
uv run lnvox voice list
```

No options. Prints the bank directory and clip count, then two tables: clips **by source** and clips **by gender × age_band** (`female/adult`, `male/young_adult`, …). An empty bank prints "Empty. Seed it via `lnvox voice seed-cv`." Check the second table before casting a book: the matcher hard-filters on exactly these buckets, a character whose bucket is empty goes uncast, and a thin bucket means many characters share a handful of voices.

## In the launcher

Neither command is run by `scripts/run_pipeline.sh`; seeding is one-time setup ([installation](/start/installation/)). The launcher only inherits `LNVOX_VOICEBANK` from your environment, passes it to every `lnvox` stage, and echoes it in the `--stop-after-audit` hints.

## Getting Common Voice

- Common Voice 23.0 and later is distributed only through the **Mozilla Data Collective**, not HuggingFace. Download the locale tarball there and extract it yourself; the loader never downloads anything. The README's setup puts the English tarball (~96 GB) under `./data/`.
- Pass the locale directory, e.g. `data/cv-corpus-NN.N-YYYY-MM-DD/en/`, which should contain `clips/` (MP3s) and the TSVs (`validated.tsv`, `train.tsv`, …).
- Install the audio dependencies with `uv sync --extra voice` (`librosa`, `soundfile`), and have a system `ffmpeg` for MP3 decoding.
- The loader expects the CV 25 metadata codes: `gender` of `male_masculine` / `female_feminine`, and `age` of `teens` … `nineties`. Rows with other values, or with fewer than 2 up-votes or any down-vote, are ineligible. See [common_voice](/library/voices/common-voice/#demographic-normalisation).
- Cost: the README estimates ~30 min of CPU/ffmpeg time for 400 speakers. `--rank-by-quality` adds roughly 10 min per ~2,500 probes (DESIGN.md §19.3).

## Inputs and outputs

```text
$LNVOX_VOICEBANK/                  default: ./voicebank
    manifest.json                  Voicebank index (rewritten by seed-cv)
    clips/cv_<client_id[:12]>.wav  mono 24 kHz PCM-16, ~12 s of speech + 0.25 s gaps
    acoustics.json                 audit cache, created by `lnvox voice audit`
```

Formats: [schema](/library/voices/schema/#file-formats). The acoustic cache: [acoustics](/library/voices/acoustics/).

## How it works

`cli.voice_seed_cv` → [`manifest.load`](/library/voices/manifest/#load) (existing bank) → [`seed_from_common_voice`](/library/voices/common-voice/#seed_from_common_voice) → merge → [`manifest.save`](/library/voices/manifest/#save).

1. **Scan** the TSV; keep eligible rows (demographics mapped, ≥ 2 up-votes, 0 down-votes, accent wanted); group by speaker.
2. **Order speakers**: by `client_id` (an arbitrary but stable slice), or, with `--rank-by-quality`, by probing one top-voted utterance per candidate, scoring bandwidth / SNR / rumble / hiss / clipping, and filling per-`(gender, age_band)` quotas (proportional to the pool, floor `max_speakers // 20`).
3. **Build** clips in that order ([`build_speaker_clip`](/library/voices/common-voice/#build_speaker_clip)): top-voted utterances, silence-trimmed, concatenated with 0.25 s gaps until ~12 s of speech, written as `clips/cv_<id>.wav`. Stop at `--max-speakers`.
4. **Merge** with the existing manifest and save.

## Merging with an existing bank

`seed-cv` always merges:

```python
existing_ids = {c.id for c in new_vb.clips}
kept = [c for c in existing.clips if c.id not in existing_ids]
merged = type(new_vb)(clips=new_vb.clips + kept)
```

- **This run's clips come first**, in selection order (best score first with `--rank-by-quality`), followed by every earlier clip this run did not produce. A clip id produced again gets its metadata and WAV replaced.
- **Nothing is ever removed.** Re-seeding with different options, or with `--rank-by-quality` over an unranked bank, gives you the union of old and new selections. To start clean, point `LNVOX_VOICEBANK` at a fresh directory, or delete the bank. Deleting clips that castings still reference makes s4 refuse to render them.
- **Growing is cheap to reason about.** Without quality ranking the selection is deterministic, so re-running with a higher `--max-speakers` selects a superset. Every selected speaker's WAV is rebuilt, though.
- **Order matters to the matcher.** The matcher shows the ranking LLM at most the first 60 clips of a `(gender, age_band)` bucket in manifest order ([matcher](/library/voices/matcher/#_soft_filter_by_accent)). Because a merge puts the latest run first, the most recent seed dominates what large buckets offer.
- **Clips added in the Studio** survive a re-seed unless the run produces the same id. A manual clip has whatever id you typed; a promoted Common Voice speaker has the seeder's `cv_` id, so if the seed also selects that speaker, its WAV and entry are rebuilt and replaced.
- **Stale acoustics.** If a re-seed rebuilds clips under the same ids with different `--target-seconds` / `--min-seconds`, delete `acoustics.json` so the audit re-measures them.

## Per-language banks

The bank directory is `Path(os.environ.get("LNVOX_VOICEBANK", "voicebank"))`, relative to the working directory (DESIGN.md §17.5). Every `voice` sub-command, `s3`, `s4` and `scenario` read it, and the Voicebank Studio honours it too (or its own `--voicebank`). Select a bank once per shell or per command:

```bash
# Seed a French bank next to the English one
LNVOX_VOICEBANK=voicebank-fr uv run lnvox voice seed-cv data/<cv-corpus-NN.N-YYYY-MM-DD>/fr/ --max-speakers 400
LNVOX_VOICEBANK=voicebank-fr uv run lnvox voice list

# Use it for a whole run
LNVOX_VOICEBANK=voicebank-fr ./scripts/run_pipeline.sh myplay --mode scenario --scenario-file "scenarios/My Play.md"
```

- A casting is tied to the bank it was made against. s4 refuses a casting whose clip ids are missing from the current bank and tells you to fix `LNVOX_VOICEBANK`. `voice cast`, by contrast, silently re-casts a prior-volume character whose clip is not in the current bank.
- Each bank keeps its own `acoustics.json`.
- `--accent` only knows the English corpus's accent names; on other locales every clip normalises to `other` or `any`, so leave the filter off.
- `LNVOX_VOICEBANK` is read straight from the process environment, not through the `Settings` `.env` file, so putting it in `.env` has no effect. Export it or prefix the command. See [environment](/reference/environment/).

## Next steps

- Audition the bank and pick a narrator by ear in the [Voicebank Studio](/guides/voicebank-studio/), then pass `--narrator-clip` to Stage V ([voice cast](/stages/voice-cast/)).
- Casting strategy and fixing collisions: [casting guide](/guides/casting/), [voice audit](/stages/voice-audit/).
