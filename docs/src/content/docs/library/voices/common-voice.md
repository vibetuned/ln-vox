---
title: lnvox.voices.common_voice
description: Seed the voicebank from a local Common Voice extraction; speaker-clip building, demographic normalisation, quality probe and bucket quotas.
sidebar:
  order: 3
  label: common_voice
---

`src/lnvox/voices/common_voice.py` turns a locally extracted Mozilla Common Voice locale directory into voicebank clips. It groups utterances by speaker (`client_id`), concatenates each speaker's top-voted utterances into one ~12 s reference WAV, and keeps their demographics. `lnvox voice seed-cv` is the bulk caller ([Building the voicebank](/stages/voicebank/)); the Voicebank Studio calls `build_speaker_clip` directly to promote or preview a single speaker; the casting audit reuses `probe_recording_quality` for its acoustic index.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`seed_from_common_voice`](#seed_from_common_voice) | function | Bulk seed: scan a TSV, pick speakers, build clips. |
| [`build_speaker_clip`](#build_speaker_clip) | function | One speaker's rows → `clips/<id>.wav` + `VoiceClip`. |
| [`probe_recording_quality`](#probe_recording_quality) | function | Bandwidth / SNR / rumble / hiss / clipping of one file. |
| [`quality_score`](#quality_score) | function | Composite score from the probe metrics. |
| [`KNOWN_ACCENT_CODES`](#known_accent_codes) | `frozenset[str]` | Every accent code the seeder can emit; validates `--accent`. |
| [`_normalize_accent`](#_normalize_accent), [`_eligible`](#_eligible) | helpers | Row normalisation and eligibility. |
| [`_bucket_quotas`](#_bucket_quotas), [`_rank_speakers_by_quality`](#_rank_speakers_by_quality) | helpers | `--rank-by-quality` selection (DESIGN.md §19). |
| `_AGE_MAP`, `_GENDER_MAP`, `_CV_ACCENT_NAME_TO_CODE` | dicts | Demographic mapping tables. |

Audio work needs the `voice` extra (`soundfile`, `librosa`) and, for MP3 decoding, a system `ffmpeg`. `_audio_libs()` imports them lazily and, if they are absent, raises a `RuntimeError` ("Voice dependencies missing") telling you to run `uv sync --extra voice`.

## Expected corpus layout

Common Voice 23.0+ is distributed only through the Mozilla Data Collective, so you download and extract the tarball yourself. Point the loader at the **locale** directory:

```text
cv-corpus-XX.X-YYYY-MM-DD/<locale>/     <- cv_root
    clips/                               MP3s, one per utterance
    validated.tsv                        preferred (already QC-passed)
    train.tsv / dev.tsv / test.tsv
    invalidated.tsv / other.tsv
```

Columns read from the TSV: `client_id`, `path` (MP3 name under `clips/`), `sentence`, `up_votes`, `down_votes`, `age`, `gender`, `accents` (falls back to `variant` when `accents` is empty).

## Demographic normalisation

### `_AGE_MAP`

| CV `age` | `age_band` |
|---|---|
| `teens` | `teen` |
| `twenties`, `thirties` | `young_adult` |
| `fourties` (CV's spelling), `fifties` | `adult` |
| `sixties`, `seventies`, `eighties`, `nineties` | `elder` |

Any other value (including empty) makes the row ineligible.

### `_GENDER_MAP`

| CV `gender` | `gender` |
|---|---|
| `male_masculine` | `male` |
| `female_feminine` | `female` |

Only these two codes are kept: matching is by vocal range, and the other CV 25 codes cover under 0.1% of speakers and span the full range. Those speakers can still be added by hand in the Studio.

:::caution
The map matches the CV 25 codes exactly. A corpus whose `gender` column says plain `male` / `female` produces zero eligible rows and an empty bank, with no error unless you also passed `--accent`.
:::

### `_normalize_accent`

```python
def _normalize_accent(s: str | None) -> str
```

Takes the first entry of a pipe-separated `accents` value (`"United States English|wolof"` → `United States English`) and maps the human-readable name through `_CV_ACCENT_NAME_TO_CODE`. Empty or missing → `"any"`; an unrecognised name → `"other"`.

| CV accent name | Code |
|---|---|
| United States English | `us` |
| England English | `england` |
| India and South Asia (India, Pakistan, Sri Lanka) | `indian` |
| Canadian English | `canada` |
| Australian English | `australia` |
| Scottish English | `scotland` |
| Southern African (South Africa, Zimbabwe, Namibia) | `african` |
| New Zealand English | `newzealand` |
| Irish English | `ireland` |
| Filipino | `philippines` |
| Hong Kong English | `hongkong` |
| Singaporean English | `singapore` |
| Malaysian English | `malaysia` |
| Welsh English | `wales` |
| West Indies and Bermuda (Bahamas, Bermuda, Jamaica, Trinidad) | `bermuda` |
| South Atlantic (Falkland Islands, Saint Helena) | `southatlandtic` (sic) |

The table only knows the English corpus's accent names. Any other label (for example from a non-English locale) comes out `other`, or `any` when the column is empty, so `--accent` filtering is only meaningful on corpora that use these names.

### `KNOWN_ACCENT_CODES`

```python
KNOWN_ACCENT_CODES: frozenset[str] = frozenset(
    set(_CV_ACCENT_NAME_TO_CODE.values()) | {"any", "other"}
)
```

The 16 mapped codes plus `any` and `other`. `seed_from_common_voice` rejects any `accents` entry outside this set up front, because an unknown code would silently filter every speaker out.

### `_eligible`

```python
def _eligible(row: dict) -> bool
```

A TSV row is kept only if `age` and `gender` are both present and mapped by the tables above, and its votes parse as integers with `up_votes >= 2` and `down_votes == 0`.

## API

### `seed_from_common_voice`

```python
def seed_from_common_voice(
    voicebank_dir: Path,
    *,
    cv_root: Path,
    tsv_name: str = "validated.tsv",
    max_speakers: int = 150,
    target_seconds: float = 12.0,
    min_seconds: float = 8.0,
    output_sr: int = 24000,
    accents: Collection[str] | None = None,
    rank_by_quality: bool = False,
    max_candidates: int | None = None,
    progress=print,
) -> Voicebank
```

Builds up to `max_speakers` reference clips and returns them as a new `Voicebank`. It writes WAVs but **not** the manifest: the caller merges the result with the existing bank and saves it (`lnvox voice seed-cv` does this, see [merging](/stages/voicebank/#merging-with-an-existing-bank)).

**Parameters**

- `voicebank_dir` — destination root; WAVs go to `voicebank_dir/clips/<id>.wav`.
- `cv_root` — the extracted locale directory (must contain `clips/`). `~` is expanded and the path resolved.
- `tsv_name` — preferred TSV, default `validated.tsv`. See [`_detect_tsv`](#_detect_tsv) for the fallback order.
- `max_speakers` — hard cap on clips written, default `150` (the same as the CLI's `--max-speakers`).
- `target_seconds` — stop adding a speaker's utterances once their trimmed speech reaches this, default `12.0`.
- `min_seconds` — drop speakers with less trimmed speech than this, default `8.0`.
- `output_sr` — sample rate of the written mono WAVs, default `24000` (not exposed on the CLI).
- `accents` — keep only these accent codes (case-insensitive, whitespace-stripped); `None` or an all-blank collection keeps every accent.
- `rank_by_quality` — pick speakers by measured recording quality with per-bucket quotas instead of `client_id` order (DESIGN.md §19).
- `max_candidates` — how many speakers to probe when ranking; `None` means `max_speakers * 6`.
- `progress` — callable for status lines (the CLI passes a dimmed console printer).

**Algorithm**

1. Validate `accents` against `KNOWN_ACCENT_CODES`, then check the audio dependencies, so both fail before the long TSV scan.
2. **Pass 1 (metadata).** Stream the TSV. Keep rows that pass `_eligible` and, if an accent filter is set, whose normalised accent is wanted (the filter is per row). Group by `client_id`.
3. **Order speakers.** Default: `sorted(client_id)`, which is stable across reruns but, since ids are hashes, an arbitrary slice with respect to quality. With `rank_by_quality`: the order returned by [`_rank_speakers_by_quality`](#_rank_speakers_by_quality).
4. **Pass 2 (audio).** Walk that order calling [`build_speaker_clip`](#build_speaker_clip) until `max_speakers` clips exist. Speakers that come back `None` are counted as "insufficient duration" and skipped; missing or undecodable MP3s are tallied separately.

**Returns** `Voicebank(clips=[…])` holding only this run's clips, in selection order.

**Raises**

- `ValueError` — unknown accent code(s); the message lists the known codes.
- `RuntimeError` — voice dependencies missing; accent filter left zero eligible speakers; or (quality ranking) no candidate's audio could be read.
- `FileNotFoundError` — `cv_root` missing or not a directory; `cv_root/clips` missing; no TSV found.

**Writes** `voicebank_dir/clips/cv_<client_id[:12]>.wav` for every selected speaker, overwriting any existing file with that name.

```python
from pathlib import Path
from lnvox.voices import manifest
from lnvox.voices.common_voice import seed_from_common_voice

vb_dir = Path("voicebank")
new = seed_from_common_voice(
    vb_dir,
    cv_root=Path("data/cv-corpus-NN.N-YYYY-MM-DD/en"),
    max_speakers=400,
    accents={"us", "england"},
    rank_by_quality=True,
)
manifest.save(vb_dir, new)   # replaces the manifest; the CLI merges instead
```

### `build_speaker_clip`

```python
def build_speaker_clip(
    speaker_id: str,
    rows: list[dict],
    *,
    clips_src: Path,
    voicebank_dir: Path,
    tsv_name: str = "manual",
    target_seconds: float = 12.0,
    min_seconds: float = 8.0,
    output_sr: int = 24000,
    on_missing=None,
) -> VoiceClip | None
```

The single definition of what a Common Voice voicebank clip is; the seeder and the Studio's "promote" / "preview merged" actions both use it. Pass a temporary `voicebank_dir` for a throwaway preview.

1. Sort `rows` by net votes (`up_votes - down_votes`), highest first.
2. For each row: load `clips_src / row["path"]` at its native rate. A missing file or a decode error calls `on_missing()` and skips the row. Trim leading/trailing silence (`librosa.effects.trim`, `top_db=30`); skip segments under 0.5 s. Resample to the first segment's rate if needed. Add the segment and its `sentence`.
3. Stop once accumulated **trimmed speech** reaches `target_seconds`.
4. Return `None` if total speech is below `min_seconds` (or nothing was usable).
5. Join segments with 0.25 s of digital silence, resample to `output_sr`, write `voicebank_dir/clips/cv_<speaker_id[:12]>.wav` as PCM-16 mono.

**Returns** a `VoiceClip` with `source="common_voice"`, `duration_seconds` = written length rounded to 0.01 s (gaps included, so it can exceed `target_seconds` noticeably when the speaker's utterances are single words), `gender` / `age_band` / `accent` taken from the top-voted row, the first three sentences used as `sample_sentences`, `license="CC0"` and `notes="Common Voice (<tsv_name>); speaker <first 8 chars>…"`.

### `_detect_tsv`

```python
def _detect_tsv(cv_root: Path, prefer: str) -> Path
```

Returns the first existing file among `prefer`, `validated.tsv`, `train.tsv`, `dev.tsv`, `test.tsv` (duplicates skipped), else raises `FileNotFoundError` naming them.

### `probe_recording_quality`

```python
def probe_recording_quality(path) -> dict | None
```

Measures one audio file (DESIGN.md §19.2). Returns `{"bandwidth", "snr", "rumble", "hiss", "clip"}` or `None` when the file is unreadable, shorter than 1.5 s, shorter than 1 s after trimming, or has fewer than 8 non-silent frames.

The signal is loaded mono at its native rate, trimmed (`top_db=30`), and analysed with a 2048-point STFT and frame RMS (hop 512). Frame levels are in dB relative to the peak sample.

- **Silence exclusion.** Only frames above `_SILENCE_FLOOR_DB = -90.0` (relative to peak) count as "real". The seeder's 0.25 s gaps are exact zeros; if they were included, every clip would score 90+ dB SNR against a floor of true silence.
- **`snr`** — 75th percentile minus 5th percentile of real frame levels (dB).
- **`bandwidth`** (Hz) — over the loud frames (real and above the 70th percentile), the mean magnitude spectrum normalised to its maximum; the highest frequency bin still above −40 dB. Separates full-band capture from 8 kHz phone/codec audio.
- **`rumble`** — share of that loud-frame spectrum below 80 Hz (hum, handling noise).
- **`hiss`** — over room-tone frames (real and within 6 dB of the 5th-percentile floor, at least 3 of them), the share of spectral magnitude above 4 kHz; `0.0` if there are fewer than 3 such frames.
- **`clip`** — fraction of samples with `|y| > 0.98 * peak`.

### `quality_score`

```python
def quality_score(m: dict) -> float
```

Composite recording-quality score, higher is better. Bandwidth and SNR carry the decision; the rest are penalties for defects that survive voice cloning:

```python
score  = min(m["bandwidth"], 11000.0) / 1000.0 * 3.0   # up to 33
score += min(m["snr"], 50.0) * 0.6                      # up to 30
score -= min(m["rumble"], 0.3) * 60.0                   # up to -18
score -= min(m["hiss"], 1.0) * 15.0                     # up to -15
score -= min(m["clip"], 0.02) * 1500.0                  # up to -30
```

It predicts recording quality, not whether a speaker sounds like a good narrator; picking among clean survivors is still done by ear.

### `_bucket_quotas`

```python
def _bucket_quotas(
    scored: list[dict], max_speakers: int
) -> dict[tuple[str, str], int]
```

Per-`(gender, age_band)` intake for quality ranking (DESIGN.md §19.4). Ranking on quality alone would inherit Common Voice's male / young-adult skew and starve the rarer buckets the matcher hard-filters on.

1. `pool[b]` = number of scored candidates in bucket `b`.
2. Floor: every bucket gets `min(max_speakers // 20, pool[b])` (5%, i.e. 20 of 400).
3. The rest, `remaining = max_speakers - sum(floors)`, is shared in proportion to each bucket's headroom (`pool[b] - quota[b]`): `quota[b] += min(headroom[b], round(remaining * headroom[b] / total_headroom))`.
4. Rounding slack is handed, one bucket at a time, to the buckets with the most unfilled candidates.

Buckets smaller than the floor contribute everything they have; if the whole pool is smaller than `max_speakers`, every candidate is taken. Returns `{}` for an empty pool.

### `_rank_speakers_by_quality`

```python
def _rank_speakers_by_quality(
    by_speaker: dict[str, list[dict]],
    *,
    clips_src: Path,
    max_speakers: int,
    max_candidates: int,
    progress,
) -> list[str]
```

The probe pass of `--rank-by-quality` (DESIGN.md §19.3). Probing one utterance is cheap; building a full clip is not, so only winners get built.

1. Candidates are the first `max_candidates` speakers in `client_id` order (not a random sample of the pool).
2. For each, probe the first readable of its three top-voted utterances with `probe_recording_quality`; unreadable speakers are dropped. Score with `quality_score`; bucket by the top-voted row's gender and age band.
3. Compute `_bucket_quotas`, take the top-scoring `quota` speakers of each bucket, and sort the picks by score.
4. Append every other scored speaker, by score, as backfill.

**Returns** client ids: picks first, then backfill. The seeder builds in this order, so when a pick fails `min_seconds` the next backfill speaker (from any bucket) takes its slot and the bank stays at `max_speakers`; the final bucket mix can therefore drift slightly from the quotas.

**Raises** `RuntimeError` if no candidate could be read at all ("Is the corpus `clips/` directory intact?").

Progress lines report every 250 probes and a final `Selected N speaker(s) across B bucket(s); median bandwidth X kHz, median SNR Y dB`.

## Notes

- **Cost.** The README estimates ~30 min of CPU/ffmpeg work to build 400 clips; DESIGN.md §19.3 puts ~2,500 probes at ~10 min on top of that.
- **Reproducibility.** Without quality ranking, the same corpus, TSV and options always select the same speakers. Raising `max_speakers` selects a superset of a smaller run.
- **Opt-in ranking.** Quality ranking changes which speakers a given `max_speakers` selects, so it is off by default (DESIGN.md §19.5). Existing castings reference clip ids; re-seeding differently adds clips but does not remove old ones (see [merging](/stages/voicebank/#merging-with-an-existing-bank)).
- `probe_recording_quality` is also the base of the audit's per-clip metrics, measured there on the full concatenated clip rather than a single utterance ([lnvox.voices.acoustics](/library/voices/acoustics/)).
