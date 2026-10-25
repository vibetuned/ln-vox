---
title: Stage L — lecture
description: lnvox lecture builds single-voice narration beats for non-fiction, with an optional LLM speech-normalize pass, replacing s1, s2 and s3 in lecture mode.
sidebar:
  order: 9
  label: L · lecture
---

Stage L is lecture mode's replacement for s1, s2 and s3. Each chapter's prose is split deterministically into narration beats of at most 375 characters, all voiced by the Narrator. An optional LLM pass then respells each beat's spoken `text` for the ear ("1973" becomes "nineteen seventy-three", "Fig. 3" becomes "Figure three"), while `source_span` stays verbatim for sync. The output is ordinary `03_directed/<chapter_id>.json`, so [s4](/stages/s4-tts/), [s5](/stages/s5-mix/) and [s6](/stages/s6-sync/) run unchanged. See the [lecture guide](/guides/lecture/) for the end-to-end workflow.

## Command

```bash
uv run lnvox lecture <book_id> [--chapters 01,02] [--no-normalize]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `BOOK_ID` (argument) | required | Book id. Reads and writes `artifacts/<book_id>/` |
| `--chapters` | all chapters | Comma-separated chapter ids |
| `--no-normalize` | off | Skip the speech-normalize pass. `text == source_span`, and no `LLMClient` is created at all |

The command exits 1 if `00_text.jsonl` is missing or the filter matches nothing. `04_voice_assignments.json` is optional. Without it, the command prints a warning and uses `DEFAULT_NARRATOR_DESCRIPTOR` (`adult, British, female, warm clear voice, measured and engaging storyteller`), which is almost never what you want. Run `lnvox voice cast <book> --narrator-clip <id>` first. The command prints the narrator descriptor in use, then `✓ <id>: N narration beat(s)` per chapter.

## In the launcher

The lecture branch decides first whether the LLM phase needs a server at all:

```bash title="scripts/run_pipeline.sh"
NEED_LLM=1
if [ "$MODE" = "lecture" ] && [ "$NO_NORMALIZE" -eq 1 ] && [ -n "$NARRATOR_CLIP" ]; then
    NEED_LLM=0
fi
```

```bash title="scripts/run_pipeline.sh"
    # Pre-retry hooks relaunch a dead vLLM — only meaningful when we manage one.
    VC_HOOK="-"; LEC_HOOK="-"
    [ "$NEED_LLM" -eq 1 ] && { VC_HOOK="ensure_vllm"; LEC_HOOK="ensure_vllm"; }
```

```bash title="scripts/run_pipeline.sh"
        banner "Stage L: lecture (split + speech-normalize → directed beats)"
        if [ "$NO_NORMALIZE" -eq 1 ]; then
            run_step "$LEC_HOOK" "Stage L (lecture)" uv run lnvox lecture "$BOOK_ID" --no-normalize
        else
            run_step "$LEC_HOOK" "Stage L (lecture)" uv run lnvox lecture "$BOOK_ID"
        fi
```

With `--mode lecture`, the launcher runs ingest (`--mode lecture`), voice cast, the casting audit, then Stage L, then s4, s5 and s6. s1, s2 and s3 are skipped.

- **`NEED_LLM=0`** applies when you pass both `--no-normalize` and `--narrator-clip <id>`. Neither remaining LLM-phase step makes an LLM call. Voice cast takes the manual Narrator override path, and the lecture ingest stub cast (`{"characters": []}`) has no one else to cast. Stage L with `--no-normalize` creates no client. So the launcher skips `prepare_llm_env` and `start_vllm` and prints `Lecture + --no-normalize + --narrator-clip: no LLM needed, skipping vLLM`. Both hooks stay `-`, meaning retries don't try to relaunch a server that was never started.
- **Otherwise** (`NEED_LLM=1`) the server is started and the hooks are `ensure_vllm`, as for the other LLM stages. That includes `--no-normalize` without `--narrator-clip`: the Narrator then has to be auto-cast (or inherited), so the server comes up for voice cast even though Stage L itself calls no LLM.
- The stage is skipped with `--skip-llm`.

## Inputs and outputs

| Path under `artifacts/<book_id>/` | Direction | Contents |
| --- | --- | --- |
| `00_text.jsonl` | read | Chapters from `lnvox ingest --mode lecture`, prose only. Code, tables and figures became visual elements at ingest |
| `04_voice_assignments.json` | read (optional) | The Narrator's `voice_descriptor` |
| `03_directed/<chapter_id>.json` | read/write (cache) | One `DirectedScene` (`<chapter_id>_s1`) of narration beats per chapter, recording the narrator descriptor and whether normalize ran |

There is an example beat on the [library page](/library/stages/lecture/#output-artifact).

## How it works

`cli.stage_lecture` → [`lecture.run`](/library/stages/lecture/#run) → [`narrator_descriptor`](/library/stages/lecture/#narrator_descriptor) → [`direct_chapter`](/library/stages/lecture/#direct_chapter) → [`split_chapter`](/library/stages/lecture/#split_chapter) → [`_normalize_beats`](/library/stages/lecture/#_normalize_beats) → [`format_prompt`](/library/stages/s3-director/#format_prompt).

1. **Descriptor.** It uses the Narrator casting's `voice_descriptor`, derived from the assigned clip, or else the default. There is no performance cue, ever.
2. **Split.** Paragraphs come from a blank-line split. Each paragraph is cut into beats of at most 375 characters at sentence ends, with the same `_split_long_text` s3 uses. A beat never spans paragraphs, and each records its 0-indexed `source_paragraph` so s6 can place code, table and figure elements.
3. **Normalize** (unless `--no-normalize`). Beats go to the LLM in batches of 25, as `[i] <source_span>`. The model returns `{index, text}`, which overwrites only `text`. The contract is to respell and never rewrite: numbers, units, symbols and abbreviations are spelled out, and nothing is paraphrased, dropped, reordered or dramatised (DESIGN.md §13.6). The prompt's examples are English; for a book in another language (the `language` of `00_book_meta.json`, which `ingest-epub` and `ingest-pdf --language` record) the prompt adds a block telling the model to respell in that language and never translate, so a French `1973` becomes `mille neuf cent soixante-treize`, not `nineteen seventy-three`. French gets worked examples; other languages are named. An English book's prompt is unchanged.
4. **Compose.** `direction = descriptor` and `prompt = format_prompt(descriptor, text)`, built from the spoken text.

## Re-running and caching

- A chapter with a parseable `03_directed/<chapter_id>.json` is reused while it matches the run. Each file records whether the normalize pass ran (`normalized`) and the narrator descriptor (`voice_descriptors`).
- A chapter whose text changed — a re-extraction (`ingest-pdf` / `ingest-epub` into the same folder), a proofread `.txt` — is rebuilt: the cached beats' verbatim `source_span`s are compared with a fresh split of the current chapter, so this needs no stored hash and also catches files written before the check existed. The log says `chapter text changed — rebuilding`.
- Toggling `--no-normalize` rebuilds each chapter with the new setting (turning normalize back on costs the LLM calls again).
- A re-cast Narrator re-voices each chapter: `direction` and `prompt` get the new descriptor, and the normalized text is kept, with no LLM call.
- A chapter file written before these records existed is treated as current and stamped with the current settings. If you changed either since, delete `03_directed/` once.
- A new narrator descriptor, or newly normalized text, changes every `prompt`, so s4 re-renders the whole book.

## Failure modes

- **Server down or dying mid-stage.** A transport error in a normalize call (server unreachable, timeout, 5xx) fails the step, so `run_step ensure_vllm` relaunches the server before the retry. Chapters finished before the crash are reused.
- **Unusable normalize answer.** A batch whose answer is bad or unparseable after the client's retries keeps its beats verbatim (`text == source_span`). The stage succeeds and caches the chapter, so a few beats may read "1973" digit by digit. To find them:

  ```python
  import json, pathlib, re

  for f in sorted(pathlib.Path("artifacts/my-book/03_directed").glob("*.json")):
      beats = [b for s in json.loads(f.read_text())["scenes"] for b in s["beats"]]
      raw = [b for b in beats if b["text"] == b["source_span"] and re.search(r"\d", b["source_span"])]
      if raw:
          print(f"{f.name}: {len(raw)} beat(s) with digits left verbatim")
  ```

  Delete the affected chapter files and re-run.
- **Missing casting.** Stage L falls back to the default narrator descriptor, but `lnvox s4` refuses to run without `04_voice_assignments.json`. Run voice cast before Stage L, so the descriptor and the clip agree.
- **Over-length beats.** A single sentence over 375 characters isn't split, and neither is text with no `.`/`!`/`?` + whitespace boundary (see [`_split_long_text`](/library/stages/s3-director/#_split_long_text)).

Design: DESIGN.md §13.3 (stage), §13.6 (normalize contract).
