---
title: lnvox.stages.s5_mix
description: Stage 5 library API — concatenate rendered beats with silence pads, loudness-normalise, and mux a chaptered .m4b with embedded pictures via ffmpeg.
sidebar:
  order: 8
  label: s5_mix
---

`lnvox.stages.s5_mix` turns the per-beat WAVs from Stage 4 into one `.m4b`: silence pads between beats, chapter concatenation, loudness normalisation, AAC encode, chapter markers and embedded pictures. It is called by `lnvox s5` ([stage page](/stages/s5-mix/)). It also owns the `.timings.json` sidecar: Stage 6, scenario-sync and `lnvox sample` read a mix's layout back through [`load_mix_layout`](#load_mix_layout) and [`find_mix_sidecar`](#find_mix_sidecar), so this module is the only definition of how a mix is laid out and which pictures it carries in what order.

All audio work is done by the system `ffmpeg` / `ffprobe` binaries. Nothing is decoded in-process.

## At a glance

| Name | Kind | Purpose |
|---|---|---|
| [`mix`](#mix) | function | Build the whole `.m4b` plus a `.timings.json` sidecar. |
| [`MixLayout`](#mixlayout), [`read_mix_layout`](#read_mix_layout) | dataclass / function | One mix's layout, parsed from its sidecar. |
| [`find_mix_sidecar`](#find_mix_sidecar), [`load_mix_layout`](#load_mix_layout) | functions | Pick the sidecar downstream stages follow: by title, else the newest. |
| [`safe_filename`](#safe_filename), `TIMINGS_SUFFIX` | function / constant | The m4b and sidecar file stem (`/`, `\` → `_`), and `".timings.json"`. |
| [`resolve_book_images`](#resolve_book_images) | function | Decide `(cover, extra_images)` for a book. s6 falls back to it for sidecars without a picture list. |
| [`embedded_image_order`](#embedded_image_order) | function | The exact stream order `_encode_m4b` embeds pictures in. |
| [`collect_images`](#collect_images) | function | Directory-listing fallback: `cover.*`/`front.*` first, then alphabetical. |
| [`SAMPLE_RATE`](#sample_rate) | constant | `48000`. Every intermediate WAV is 48 kHz stereo `pcm_s16le`. |
| `_make_silence`, `_build_chapter_concat_items`, `_concat_to_wav`, `_loudnorm`, `_build_ffmetadata`, `_encode_m4b`, `_run` | private | The ffmpeg steps, described under [The ffmpeg pipeline](#the-ffmpeg-pipeline). |

## API

### `mix`

```python
def mix(
    *,
    chapters_audio: list[ChapterAudio],
    chapter_titles: dict[str, str],
    book_title: str,
    beats_root: Path,
    output_dir: Path,
    work_dir: Path | None = None,
    intra_silence: float = 0.25,
    inter_scene_silence: float = 1.0,
    inter_chapter_silence: float = 2.0,
    lead_in_silence: float = 0.0,
    target_lufs: float = -18.0,
    aac_kbps: int = 96,
    cover_image: Path | None = None,
    images: list[Path] | None = None,
    audio_dir: str | None = None,
    keep_work: bool = False,
    progress: Callable[[str], None] = print,
) -> Path
```

Mixes the whole book into a single `.m4b` and returns its path. Every argument is keyword-only.

**Parameters**

- `chapters_audio`: one [`ChapterAudio`](/library/tts/schema/) manifest per chapter, in playback order. The CLI builds this list from `05_audio/<chapter>/manifest.json`, with directories sorted by name. Each manifest's `beats` are also in playback order.
- `chapter_titles`: maps `chapter_id` to the title shown in the chapter marker. When an id is missing, the id itself is used as the title.
- `book_title`: written as the m4b's `title` metadata and used as the output filename. `/` and `\` are replaced with `_` in the filename only, so `level99/volume-01` becomes `level99_volume-01.m4b`.
- `beats_root`: the directory each beat's `wav_path` is resolved against. The CLI passes `artifacts/<book>/`, since Stage 4 stores `wav_path` relative to the book dir (for example `05_audio/01/01_s1_b0000.wav`). If `beats_root / wav_path` does not exist, `beats_root.parent / wav_path` is tried next.
- `output_dir`: receives the `.m4b` and `.timings.json`. It is created if missing. The CLI passes `artifacts/<book>/06_final/`.
- `work_dir`: holds intermediate files. Default `output_dir / "_work"`, which is removed after a successful mix. An explicit `work_dir` belongs to the caller and is never removed.
- `intra_silence`: pad between two consecutive beats of the same `scene_id`. Default `0.25` s.
- `inter_scene_silence`: pad between two consecutive beats whose `scene_id` differs. Default `1.0` s. In scenario mode the directed "scenes" are staging-delimited dialogue runs, so this pad is the staging pause.
- `inter_chapter_silence`: pad between chapters. Default `2.0` s. Scenario mode uses `0.75`.
- `lead_in_silence`: extra silence placed before every beat, including the first beat of each chapter. Default `0.0`, meaning none. Scenario mode uses `0.35` (see [Lead-in](#lead-in)).
- `target_lufs`: integrated loudness target for `loudnorm`. Default `-18.0`.
- `aac_kbps`: AAC bitrate in kbit/s. Default `96`.
- `cover_image`: embedded as the first picture. If both `cover_image` and `images` are given, the cover is placed first and duplicates are removed.
- `images`: extra pictures to embed after the cover. Missing files are skipped.
- `audio_dir`: the name of the render directory the manifests came from (`"05_audio"` or `"05_audio_v2"`). It is only recorded in the sidecar, so Stage 6 and scenario-sync can refuse to time a VibeVoice mix. The CLI passes it. Default `None`, recorded as `null`.
- `keep_work`: keep the default work directory after success. Default `False`. It is always kept when the mix fails.
- `progress`: receives one-line status messages. Default `print`.

**Returns** the path of the written `.m4b`.

**Raises**

- `FileNotFoundError` if a beat WAV exists at neither candidate path. The message is `Beat WAV not found at … (or …)`. This is what you see after [`lnvox audio purge`](/stages/utilities/#lnvox-audio-purge) and before s4 has re-rendered.
- `RuntimeError` if any ffmpeg or ffprobe call fails. `_run` rewrites `CalledProcessError` so the message contains the failing command and the last 40 lines of stderr.

**Writes**

| Path | Content |
|---|---|
| `<output_dir>/<safe_title>.m4b` | The audiobook. |
| `<output_dir>/<safe_title>.timings.json` | [Sidecar](#timingsjson-sidecar) with the full layout of the mix. Written after the encode succeeds. |
| `<work_dir>/sil_intra_<ms>ms.wav`, `sil_scene_<ms>ms.wav`, `sil_chapter_<ms>ms.wav`, `sil_lead_<ms>ms.wav` | Silence pads. The name encodes `int(seconds * 1000)`. A pad file that already exists in the work dir is reused. |
| `<work_dir>/chapter_<id>.wav`, `chapter_<id>_list.txt` | One concatenated WAV per chapter and its concat-demuxer list. |
| `<work_dir>/book_raw.wav`, `book_raw_list.txt` | The whole book before normalisation. |
| `<work_dir>/book_normalized.wav` | After `loudnorm`. |
| `<work_dir>/ffmetadata.txt` | Chapter markers. |

The work directory holds full-length uncompressed WAVs: every chapter, the raw book and the normalised book. At 48 kHz stereo 16-bit that is about 690 MB per hour of audio for each full-book copy. Nothing reads it across runs, so the default `_work/` is deleted once the sidecar is written. Pass `keep_work=True` (`lnvox s5 --keep-work`) to inspect it.

**Example**

```python
from pathlib import Path
from lnvox.stages import s5_mix
from lnvox.tts.schema import ChapterAudio

book = Path("artifacts/level99/volume-01")
chapters = [
    ChapterAudio.model_validate_json((d / "manifest.json").read_text())
    for d in sorted((book / "05_audio").iterdir())
    if (d / "manifest.json").exists()
]
cover, extra = s5_mix.resolve_book_images(book, Path("novels/level99/volume-01"))
m4b = s5_mix.mix(
    chapters_audio=chapters,
    chapter_titles={"01": "Chapter 1"},
    book_title="level99/volume-01",
    beats_root=book,
    output_dir=book / "06_final",
    cover_image=cover,
    images=extra,
    audio_dir="05_audio",
)
```

### `MixLayout`

```python
@dataclass
class MixLayout:
    sidecar: Path
    m4b: Path
    book_title: str
    intra_silence: float
    inter_scene_silence: float
    inter_chapter_silence: float
    lead_in_silence: float
    chapter_ids: list[str]
    chapter_bounds: dict[str, tuple[float, float]]
    audio_dir: str | None = None
    embedded_images: list[Path] | None = None
```

One Stage-5 run's layout, read back from its sidecar. Stage 6 and scenario-sync never probe audio, so they must replay this layout exactly, and `lnvox sample` must slice the m4b it describes. They used to take the layout from their own flags, kept in step with the s5 run by hand, and an s5 `--lead-in`, `--chapters` subset or `--cover` / `--images-dir` override silently skewed every timing or picture slot downstream.

- `m4b`: `sidecar.parent / <recorded m4b name>`, or the sidecar's stem plus `.m4b` for an old sidecar.
- `chapter_ids`: the chapters actually mixed, in mix order. `chapter_bounds` maps each to its probed `(start_seconds, end_seconds)` in the m4b.
- `audio_dir`: `"05_audio"` or `"05_audio_v2"`. `None` for a sidecar written before it was recorded.
- `embedded_images`: the pictures in embed order, cover first. `None` for a sidecar written before it was recorded. Callers then fall back to [`resolve_book_images`](#resolve_book_images) without overrides.

### `read_mix_layout`

```python
def read_mix_layout(sidecar: Path) -> MixLayout
```

Parses a sidecar of any version. Missing pad keys take the s5 defaults (`0.25`, `1.0`, `2.0`), and a missing `lead_in_silence` is `0.0`, which is what every mix used before scenario mode.

### `find_mix_sidecar`

```python
def find_mix_sidecar(final_dir: Path, title: str | None = None) -> Path | None
```

The sidecar that downstream stages should follow.

- With `title` (an `lnvox s5 --title`): exactly `<safe_filename(title)>.timings.json`, or `None` if it does not exist.
- Without: the **newest** `*.timings.json` in `final_dir` by modification time, so a stale m4b left by an earlier `--title` is not picked. `None` if there is none, or `final_dir` does not exist.

The sidecar is selected rather than the m4b because it is written only after the m4b is fully encoded.

### `load_mix_layout`

```python
def load_mix_layout(final_dir: Path, title: str | None = None) -> MixLayout | None
```

`read_mix_layout(find_mix_sidecar(final_dir, title))`, as Stage 6 and scenario-sync use it. It returns `None` when there is no sidecar at all, so the caller falls back to its flag defaults. An explicit `title` with no sidecar raises `FileNotFoundError` naming the mixes that do exist, instead of quietly following a different mix.

### `safe_filename`

```python
def safe_filename(book_title: str) -> str
```

The file stem shared by `<title>.m4b` and `<title>.timings.json`: `/` and `\` become `_`.

### `resolve_book_images`

```python
def resolve_book_images(
    book_dir: Path,
    novel_dir: Path,
    *,
    cover: Path | None = None,
    images_dir: Path | None = None,
    log=None,
) -> tuple[Path | None, list[Path]]
```

Returns `(cover_image, extra_images)` exactly as Stage 5 will embed them. ln-reader pairs `sync_manifest.images[i]` with the i-th embedded picture **by position**, because MP4's `covr` atom stores no per-picture name. That is why [Stage 6](/library/stages/s6-sync/#align_images_to_embeds) and [`sample`](/library/stages/sample/) call this function instead of working the order out themselves (DESIGN.md §23.3).

Resolution order:

1. **Cover.** An explicit `cover` wins. Otherwise the function reads `cover_image` from `book_dir/00_book_meta.json`, which `lnvox ingest` copies from `.epub_meta.json` with the path made absolute. If that file is missing, a warning is logged and the cover is dropped.
2. **Extra images.** The directory is `images_dir` if given, else `novel_dir/images`. If it exists and `images_dir` was **not** given, the spine-ordered `images` list from `novel_dir/.epub_meta.json` is used, keeping only files that exist. That is the order a reader meets the illustrations. If that list yields nothing, the function falls back to [`collect_images`](#collect_images) on the directory, which would put `Insert10` before `Insert2`.
3. **Cover promotion.** If there is still no cover, the first extra image whose stem is `cover` or `front` (any case) is promoted to cover and removed from the extras.

`log` is any callable taking a string (the CLI passes `console.print`). It defaults to a no-op.

Stage 6 does not call this for a current sidecar. It uses the `embedded_images` list the mix recorded, which reflects `--cover` and `--images-dir`. It calls `resolve_book_images(book_dir, novel_dir)`, with no overrides, only for a sidecar written before that list existed, or when there is no sidecar.

### `embedded_image_order`

```python
def embedded_image_order(
    cover_image: Path | None, images: list[Path] | None
) -> list[Path]
```

Returns the pictures `_encode_m4b` embeds, in stream order: the cover first if it exists, then `images`. Files that do not exist are skipped, and duplicates are removed by resolved path. This mirrors the encoder's own loop. `mix` records this list as the sidecar's `embedded_images`, and Stage 6 aligns the manifest `images` against it.

### `collect_images`

```python
def collect_images(images_dir: Path) -> list[Path]
```

Lists `*.jpg`, `*.jpeg`, `*.png` and `*.webp` files in `images_dir` (not recursive; extensions matched in any case). Files whose stem is `cover` or `front` come first, then the rest in sorted order. Returns `[]` if the directory does not exist.

### `SAMPLE_RATE`

```python
SAMPLE_RATE = 48000
```

The sample rate of every silence pad and every concat or loudnorm output (always `-ac 2`, `pcm_s16le`).

## The ffmpeg pipeline

`mix` runs these steps in order. Each one is a separate ffmpeg process started through `_run`.

1. **Silence pads.** `_make_silence` generates stereo silence at 48 kHz:

   ```bash
   ffmpeg -y -hide_banner -loglevel error -f lavfi \
     -i anullsrc=channel_layout=stereo:sample_rate=48000 \
     -t 0.250 -c:a pcm_s16le _work/sil_intra_250ms.wav
   ```

   Three pads are always made (intra, inter-scene, inter-chapter). A fourth is made only when `lead_in_silence > 0`.

2. **Per-chapter concat.** `_build_chapter_concat_items` interleaves beats and pads:
   - before every beat except the chapter's first, the inter-scene pad if `beat.scene_id` differs from the previous beat's, otherwise the intra pad;
   - then the lead-in pad, if any;
   - then the beat WAV.

   The list is written for the concat demuxer (absolute paths, single quotes escaped) and rendered with:

   ```bash
   ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i chapter_01_list.txt \
     -c:a pcm_s16le -ar 48000 -ac 2 _work/chapter_01.wav
   ```

   The chapter WAV's duration is **probed** with `ffprobe -show_entries format=duration`. The chapter's start offset is the running cursor. The cursor then advances by the chapter duration, plus `inter_chapter_silence` if this is not the last chapter.

3. **Book concat.** All chapter WAVs are joined with the inter-chapter pad between them into `book_raw.wav`, using the same concat command.

4. **Loudness.** Single-pass EBU R128 normalisation:

   ```bash
   ffmpeg -y -i book_raw.wav -af loudnorm=I=-18.0:LRA=11:TP=-2 \
     -c:a pcm_s16le -ar 48000 -ac 2 book_normalized.wav
   ```

   A two-pass loudnorm would be about 0.5 LU more accurate and take twice as long. The module docstring judges that not worth it for audiobooks.

5. **Chapter metadata.** `_build_ffmetadata` writes one `[CHAPTER]` block per chapter, with millisecond times taken from the cursor in step 2:

   ```text title="06_final/_work/ffmetadata.txt"
   ;FFMETADATA1
   title=level99/volume-01

   [CHAPTER]
   TIMEBASE=1/1000
   START=0
   END=1502113
   title=Chapter 1: The Tutorial Floor

   [CHAPTER]
   TIMEBASE=1/1000
   START=1504113
   END=2824595
   title=Chapter 2: Party of One
   ```

6. **Encode and mux.** `_encode_m4b` encodes AAC and attaches pictures:

   ```bash
   ffmpeg -y -hide_banner -loglevel error \
     -i book_normalized.wav -i ffmetadata.txt -i cover.jpg -i insert1.jpg \
     -map 0:a -map 2:v -map 3:v -map_metadata 1 \
     -c:a aac -b:a 96k -c:v copy \
     -disposition:v:0 attached_pic -metadata:s:v:0 title=cover \
     -disposition:v:1 attached_pic -metadata:s:v:1 title=insert1 \
     -movflags +faststart -f mp4 level99_volume-01.m4b
   ```

   - Input 0 is the audio and input 1 the ffmetadata file (global title and chapters come from `-map_metadata 1`). Pictures start at input index 2.
   - Each picture becomes its own video stream, stream-copied (not re-encoded), marked `attached_pic`, with a `title` tag equal to the file stem so external tools can tell streams apart.
   - `+faststart` moves the `moov` atom to the front so the file can stream.

## `timings.json` sidecar

The layout contract of one mix, written next to the m4b once the encode has succeeded, so a sidecar always describes a complete m4b. `lnvox s6`, `lnvox scenario-sync` and `lnvox sample` read it back through [`load_mix_layout`](#load_mix_layout) / [`find_mix_sidecar`](#find_mix_sidecar). `total_duration_seconds` is the probed duration of `book_raw.wav` and is not rounded. Chapter offsets are rounded to milliseconds.

```json title="artifacts/level99/volume-01/06_final/level99_volume-01.timings.json"
{
  "book_title": "level99/volume-01",
  "m4b": "level99_volume-01.m4b",
  "audio_dir": "05_audio",
  "total_duration_seconds": 2824.595312,
  "target_lufs": -18.0,
  "intra_silence": 0.25,
  "inter_scene_silence": 1.0,
  "inter_chapter_silence": 2.0,
  "lead_in_silence": 0.0,
  "embedded_images": [
    "/home/me/ln-vox/novels/level99/volume-01/images/cover.jpg",
    "novels/level99/volume-01/images/insert1.jpg"
  ],
  "chapters": [
    {
      "chapter_id": "01",
      "title": "Chapter 1: The Tutorial Floor",
      "start_seconds": 0.0,
      "end_seconds": 1502.113,
      "duration_seconds": 1502.113
    },
    {
      "chapter_id": "02",
      "title": "Chapter 2: Party of One",
      "start_seconds": 1504.113,
      "end_seconds": 2824.595,
      "duration_seconds": 1320.482
    }
  ]
}
```

| Key | Meaning | Read by |
|---|---|---|
| `book_title` | The `--title` as given, slashes included. | |
| `m4b` | File name of the m4b in the same directory. | s6 copies it into the manifest as `m4b`, and `sample` slices that file. |
| `audio_dir` | `05_audio`, or `05_audio_v2` for a `--v2` mix. `null` when a library caller did not pass it. | s6 and scenario-sync refuse a `05_audio_v2` mix. |
| `intra_silence`, `inter_scene_silence`, `inter_chapter_silence`, `lead_in_silence` | The four pad values used. | s6 and scenario-sync, as the defaults of their pad flags. |
| `embedded_images` | The pictures in embed order, cover first, as passed to ffmpeg (absolute or relative to the working directory). Only the file names matter downstream. | s6, for the manifest `images` slots. |
| `chapters[]` | The chapters actually mixed, in order, each with its probed start, end and duration in the m4b. | s6 times only these chapters and starts each at its `start_seconds`. scenario-sync emits only these scenes. |
| `total_duration_seconds`, `target_lufs` | For debugging and re-encodes. | |

**Older sidecars.** Sidecars written before `m4b`, `audio_dir` and `embedded_images` existed still work. The m4b name is derived from the sidecar's stem, the render directory is assumed to be `05_audio`, and Stage 6 re-resolves the pictures with [`resolve_book_images`](#resolve_book_images) without overrides, which was the old behaviour. The oldest sidecars also lack `lead_in_silence`, which is read as `0.0`.

## Lead-in

`lead_in_silence` exists for scenario mode (DESIGN.md §17.4). The 2reply reader app polls the playback position only every ~0.3 s on older devices. A scenario sync entry's `start` points at the **beginning** of the lead-in, so the poll always fires while the line is still silent and the highlight never arrives after the words have started. This matters more after the edge-silence trim in Stage 4 ([trim](/library/tts/trim/)), because trimmed beats start speaking immediately. Books keep `0.0`.

With a lead-in, one chapter's concat list looks like this:

```text
[lead][beat 1] [intra][lead][beat 2] [inter-scene][lead][beat 3] …
```

## Notes

- **Timing contract.** Stage 6 ([`_compute_beat_timings`](/library/stages/s6-sync/#_compute_beat_timings)) and scenario-sync ([`_beat_times`](/library/stages/scenario-sync/#_beat_times)) rebuild the same layout from `duration_seconds` in the manifests. They never probe audio. They take the pads from the sidecar by default, so the values agree unless a sync flag overrides them. See the [layout contract](/stages/s5-mix/#the-layout-contract) on the stage page.
- **Probed vs summed durations.** `mix` uses probed chapter durations for the chapter markers. Stage 6 sums the per-beat `duration_seconds`, which are rounded to milliseconds, but starts every chapter at the probed `start_seconds` in the sidecar, so rounding cannot accumulate across chapters. If a chapter's summed length differs from its probed length by more than 0.5 s, its renders changed after the mix, and Stage 6 prints a `[WARN]`.
- **Beat WAV format.** The concat demuxer needs every input in one stream format. Silence pads are 48 kHz stereo `pcm_s16le`, and so is every Stage-4 backend's output (Dramabox, and VibeVoice, which resamples to 48 kHz). A beat WAV in any other format is concatenated wrongly and without an error.
- **Not incremental.** Every call rebuilds all chapter WAVs, the book WAV, the loudnorm pass and the encode. Nothing is reused across calls, since the default work directory is deleted after each successful mix. Runtime scales with book length (minutes for a full novel), and there is no per-chapter cache.
