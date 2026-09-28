---
title: Stage 6 — EPUB sync
description: Wrap every beat of the source EPUB in a data-beat-id span and emit sync_manifest.json so a reader can highlight text and flip images in time with the m4b.
sidebar:
  order: 13
  label: 6 · EPUB sync
---

Stage 6 lets a reader app follow along. It re-aligns the directed beats onto the **original** EPUB's XHTML and writes three files:

- a copy of the EPUB in which each beat's text is wrapped in `<span class="lnvox-beat" data-beat-id="…">`;
- a `sync_manifest.json` that maps each beat id to its start and end time in the m4b;
- `unmatched.json`, listing beats that could not be placed.

The manifest also places every illustration, and in lecture mode every code, table or figure block, at a playback time. The stage is CPU-only and optional. Narration and lecture books use it. Scenario mode uses [scenario-sync](/stages/scenario-sync/) instead.

## Command

```bash
uv run lnvox s6 <book_id> [--epub PATH] [--novels-root DIR] [--title T] [--intra S] [--inter-scene S] [--inter-chapter S] [--lead-in S]
```

| Option | Default | Meaning |
|---|---|---|
| `BOOK_ID` (argument) | required | Book id. Artifacts come from `artifacts/<book_id>/`. |
| `--epub` | `epubs/<book_id>.epub` | The source EPUB, for example `epubs/level99/volume-01.epub`. |
| `--novels-root` | `novels` | Where `<book_id>/.epub_meta.json` lives, and `<book_id>/images/` for books whose mix recorded no picture list. |
| `--title` | the newest mix | Which mix to sync against: the `lnvox s5 --title` of `06_final/<title>.m4b`. An unknown title exits 1 and lists the mixes present. |
| `--intra` | recorded | Override the intra-scene silence. Default: the value in the mix's `.timings.json`, else `0.25`. |
| `--inter-scene` | recorded | Override the inter-scene silence. Default: the recorded value, else `1.0`. |
| `--inter-chapter` | recorded | Override the inter-chapter silence. Default: the recorded value, else `2.0`. |
| `--lead-in` | recorded | Override the per-beat lead-in. Default: the recorded value, else `0`. |

Stage 6 follows the Stage-5 mix it syncs against: its pads, its lead-in, the chapters it contained and the order it embedded pictures in, all read from `06_final/<title>.timings.json`. None of the s5 flags need repeating. A pad flag overrides the recorded value and prints a `[WARN]` when the two differ, because the timings then no longer describe that m4b. See [the layout contract](/stages/s5-mix/#the-layout-contract).

At the end the CLI prints `Matched N/M beats (P%)`: green when nothing is unmatched, yellow otherwise, with a pointer to `unmatched.json`.

## In the launcher

```bash title="scripts/run_pipeline.sh"
if [ "$SKIP_SYNC" -eq 1 ]; then
    banner "Skipping sync phase (--skip-sync)"
elif [ "$MODE" = "scenario" ]; then
    # … scenario-sync, see the scenario-sync page …
elif [ "$TTS_BACKEND" = "vibevoice" ]; then
    # A session WAV has no per-beat timestamps for the reader to anchor to;
    # recovering them needs forced alignment — future work (DESIGN.md §16.10).
    banner "Skipping sync phase (VibeVoice v2 audio has no per-beat timing)"
else
    SYNC_EPUB="${EPUB_PATH:-epubs/$BOOK_ID.epub}"
    if [ ! -f "$SYNC_EPUB" ]; then
        banner "Stage 6: skipped (no source EPUB at $SYNC_EPUB)"
        echo "Place the EPUB at epubs/$BOOK_ID.epub or pass --epub <path> to enable sync."
    elif [ ! -d "$BOOK_ART/05_audio" ]; then
        banner "Stage 6: skipped (no rendered audio at $BOOK_ART/05_audio)"
    else
        banner "Stage 6: sync layer (EPUB beat-spans + sync_manifest.json)"
        run_step - "Stage 6 (sync)" uv run lnvox s6 "$BOOK_ID" \
            --epub "$SYNC_EPUB" --novels-root "$NOVELS_ROOT"
    fi
fi
```

Stage 6 is the launcher's last step. It is **skipped** in these cases:

- `--skip-sync` is given;
- `--mode scenario`: scenario-sync runs instead;
- `--tts-backend vibevoice`;
- no EPUB at `epubs/<book_id>.epub`, and no `--epub <path>` given. A `.txt`-only book lands here;
- no `$LNVOX_ARTIFACTS_DIR/<book_id>/05_audio/` (default `artifacts/`), because TTS has not run.

No pad flags and no `--title` are passed, so s6 follows the newest mix, which is the one the launcher's s5 step has just written. The step goes through `run_step -` (retries, no hook).

:::note
The launcher's pre-check looks in `$BOOK_ART/05_audio`, i.e. under `LNVOX_ARTIFACTS_DIR` read from the process environment. A root set only in `.env` moves the CLI's artifacts but not the launcher's check, which then skips s6. Export the variable instead.
:::

## Inputs and outputs

**Reads**

| Path | Used for |
|---|---|
| The source EPUB (`--epub`) | OPF manifest and spine, every XHTML page. Never modified. |
| `novels/<book>/.epub_meta.json` | Chapter id to XHTML `source_parts`, lecture `visual_elements`, spine-ordered `images`. Written by [`lnvox ingest-epub`](/stages/ingest-epub/). |
| `artifacts/<book>/03_directed/*.json` | Beat `text`, `source_span`, `type`, `speaker`, `source_paragraph`. |
| `artifacts/<book>/06_final/<title>.timings.json` | The mix's layout: pads, lead-in, chapter list and measured chapter starts, embedded pictures in order, render directory, m4b name. The newest sidecar unless `--title` is given. |
| `artifacts/<book>/05_audio/<chapter>/manifest.json` | Per-beat `duration_seconds` and `scene_id` of each mixed chapter, for timing. |
| `artifacts/<book>/00_book_meta.json`, `novels/<book>/images/` | Only when the sidecar has no picture list (older sidecars) or there is no sidecar: which pictures the m4b embeds, and in what order. |

**Writes** to `artifacts/<book>/07_sync/`:

| File | Content |
|---|---|
| `<book_id with / → _>.epub` | The source EPUB with beat spans, plus a responsive-image `<style>` on every XHTML page. `mimetype` is written first and stored. Every non-XHTML entry is byte-identical. |
| `sync_manifest.json` | `beats[]` with start and end seconds and match confidence, `images[]` (one per embedded m4b picture, in order), `visual_elements[]`, the `match_confidence` histogram, the `silences` used (lead-in included) and the `m4b` they describe. [Full schema and example](/library/stages/s6-sync/#sync_manifestjson). |
| `unmatched.json` | `{"beats": [{beat_id, chapter_id, text_preview, reason}]}`. [Example](/library/stages/s6-sync/#unmatchedjson). |

## How it works

`cli.stage6` → [`s6_sync.run`](/library/stages/s6-sync/#run), which calls in turn:

- [`s5_mix.load_mix_layout`](/library/stages/s5-mix/#load_mix_layout);
- `_parse_opf`;
- [`_compute_beat_timings`](/library/stages/s6-sync/#_compute_beat_timings);
- per chapter, [`_build_shadow`](/library/stages/s6-sync/#_build_shadow), then Pass 1 ([`_find_source_span`](/library/stages/s6-sync/#_find_source_span) and strict [`_find_beat`](/library/stages/s6-sync/#_find_beat)), then Pass 2 ([`_find_beat_in_range`](/library/stages/s6-sync/#_find_beat_in_range)), then [`_wrap_matches`](/library/stages/s6-sync/#_wrap_matches);
- spine image pages;
- [`place_visual_element`](/library/stages/s6-sync/#place_visual_element);
- [`align_images_to_embeds`](/library/stages/s6-sync/#align_images_to_embeds), against the sidecar's `embedded_images` (or [`s5_mix.resolve_book_images`](/library/stages/s5-mix/#resolve_book_images) for older sidecars);
- [`_write_modified_epub`](/library/stages/s6-sync/#_write_modified_epub);
- [`_count_beat_spans`](/library/stages/s6-sync/#_count_beat_spans).

In steps:

1. **Timing.** The beat times are rebuilt from the Stage-4 manifests with the layout the mix recorded: intra, inter-scene and inter-chapter pads, the lead-in before every beat, and only the chapters that were mixed, in mix order. Each chapter starts at the offset s5 measured for it. No audio is probed. Directed chapters that are not in the mix are skipped.
2. **Shadow.** For each chapter, all of its XHTML parts are concatenated into one normalised *master shadow* (lower-cased, straight quotes, `-` for dashes, collapsed whitespace), with a map back to `(text node, offset)`.
3. **Pass 1, strict and forward.** Each beat's verbatim `source_span` is matched exactly within the next 8 000 characters (`span-exact`, the common path). If that fails, the beat's `text` is tried with `exact` (short beats) or `anchored` (30-char head and tail) matching. The cursor advances only on a hit.
4. **Pass 2, lenient gap-fill.** Beats that are still missing are searched only inside the gap between their neighbours' matches, with 2 000 characters of backward slack, so a beat that Stage 2 moved after its neighbour is still found. The search tries `span-exact`, then `exact` / `anchored`, then `head-only`, `tail-only` and fuzzy. There are up to 3 rounds.
5. **Wrap.** Matches are cut into the DOM, one span per text node, with a shared `data-beat-id` for beats that cross nodes. One DOM per XHTML file is shared by all chapters on that page.
6. **Images.** Inline images get the neighbouring beats and a trigger time (a tailpiece triggers as the last beat ends). Image-only spine pages are placed between parts. `images[]` is then aligned 1:1 with the pictures the mix embedded, in the order the sidecar records.
7. **Repack and verify.** The EPUB is rewritten and the spans that actually landed are counted. A `[WARN]` is printed when fewer than 75% of matched beats made it into the EPUB.

The algorithm, all confidence values and the tuning constants are documented on the [library page](/library/stages/s6-sync/#the-matching-ladder).

## Player integration

A reader needs only the synced EPUB and `sync_manifest.json`. Render the EPUB normally, then on each playback tick highlight the spans of the active beat:

```js
// manifest = await (await fetch("sync_manifest.json")).json();
// Beats are in playback order; spans of one beat share its data-beat-id.
let current = null;

function onTimeUpdate(t) {
  const beat = manifest.beats.find(
    (b) => b.start_seconds <= t && t < b.end_seconds
  );
  const id = beat ? beat.data_beat_id : null;
  if (id === current) return;
  if (current) {
    document
      .querySelectorAll(`[${manifest.data_attr}="${current}"]`)
      .forEach((el) => el.classList.remove("active"));
  }
  if (id) {
    const els = document.querySelectorAll(`[${manifest.data_attr}="${id}"]`);
    els.forEach((el) => el.classList.add("active"));
    els[0]?.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  current = id;
}
```

```css
.lnvox-beat.active { background: rgba(255, 220, 0, 0.35); }
```

- `beat.xhtml` names the spine page where the match starts, so load that page before you query it.
- **Pictures.** `images[i]` describes the i-th embedded picture of the m4b, and index 0 is the cover. Show it when playback reaches `images[i].trigger_seconds`. A `null` trigger means no beat reaches that picture.
- **Lecture elements.** `visual_elements[]` lists every occurrence, including lecture `code` / `table` / `figure` / `footnote` / `equation` blocks, with `after_beat_id`, `before_beat_id` and `trigger_seconds`. Blocks with a `src` PNG use the same image path. Footnotes and equations carry `html`.
- **Silences.** Between beats (in the pads) no beat is active. Keep the last highlight if you want no flicker.
- **Lead-in.** In a mix with a lead-in, a beat's `start_seconds` is where its lead-in starts, so the highlight lands just before the speech, as in [scenario-sync](/stages/scenario-sync/). `end_seconds` is where the speech ends.

## Re-running and caching

- Deterministic and uncached: every run rebuilds all three outputs from scratch in seconds to a few minutes.
- Re-run after anything that changes timing or text: s4 re-renders, any s5 re-mix, s2/s3 re-runs, or a new EPUB.
- Stage 6 does not need the m4b file itself, only its sidecar. With no sidecar at all it prints `No 06_final/*.timings.json …` and assumes the s5 defaults (or the given pads) and every chapter in `05_audio/`, which is the pre-sidecar behaviour.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| `Missing …/03_directed. Run lnvox s3 first.` / `Missing …/05_audio. Run lnvox s4 first.` (exit 1) | Earlier stages have not run. VibeVoice books have only `05_audio_v2/` and cannot be synced. |
| `EPUB not found at … Pass --epub <path>.` (exit 1) | Place the EPUB at `epubs/<book_id>.epub` or pass `--epub`. |
| `FileNotFoundError: Missing …/.epub_meta.json; was this book imported via lnvox ingest-epub?` | The book was ingested from plain `.txt`, or `--novels-root` is wrong. |
| `[skip] 07: no source_parts in .epub_meta.json` | That chapter's beats all go to `unmatched.json` with reason `no source_parts for chapter`. |
| `[WARN] only N of M matched beat(s) reached the EPUB` | Spans were lost while writing, not while matching (DESIGN.md §21.1). Healthy books land at 88–97%. |
| Many `unmatched` / `fuzzy` / `tail-only` | Stage 2 paraphrased or hallucinated. Check the `span-exact` share in `match_confidence`, and `unmatched.json`. |
| `No mix titled 'X': …/06_final/X.timings.json does not exist (mixes here: …)` (exit 1) | `--title` names no mix. Use one of the listed titles, or omit `--title` to follow the newest. |
| `… was mixed from 05_audio_v2/ (VibeVoice sessions), which has no per-beat timing` (exit 1) | The newest (or named) mix is a `--v2` mix. Pass `--title` of a mix made from `05_audio/`. |
| `…/05_audio/<id>/manifest.json is missing, but chapter <id> is part of the mix` (exit 1) | A mixed chapter's renders were deleted. Re-render and re-mix. |
| `[WARN] chapter 24: its renders are +648.7 s off the mixed chapter …` | That chapter was re-rendered after the mix. Its beat times will not match the m4b; the other chapters are unaffected. Re-run s5, then s6. |
| `[WARN] intra = 0.3 s overrides the 0.25 s … was mixed with` | An explicit pad flag differs from the mix. Drop the flag unless the timings are meant for a different m4b. |
| Highlights drift later and later into the book | Only possible without a sidecar, or with overriding pad flags. Compare `silences` in the manifest with `06_final/*.timings.json`. |
