---
title: Stage 1 — character extraction
description: lnvox s1 builds the book's cast (01_characters.json) from per-chapter LLM extraction, deterministic clustering and an LLM merge-groups pass.
sidebar:
  order: 4
  label: 1 · characters
---

Stage 1 produces `01_characters.json`, the cast of the book. Stage 2 uses it for speaker tags, [voice cast](/stages/voice-cast/) matches each character to a reference clip, and s3 writes a voice descriptor per character. It runs only in narration mode. Lecture mode writes a Narrator-only stub at ingest, and scenario mode gets its cast from [ingest-scenario](/stages/ingest-scenario/).

## Command

```bash
uv run lnvox s1 <book_id> [--chapters 01,02,05]
```

| Option | Default | Meaning |
| --- | --- | --- |
| `BOOK_ID` (argument) | required | Book id, `<series>/<volume-NN>` or flat. Artifacts are read and written under `artifacts/<book_id>/` |
| `--chapters` | all chapters | Comma-separated chapter ids to process. The cast written is the cast **of these chapters only** |

The command exits 1 if `00_text.jsonl` is missing or the filter matches no chapter. It prints the LLM endpoint and model, any prior volumes found in the series, one `✓ <chapter_id>: found N character(s)` line per chapter (the count after sanitisation), and a summary table of the final cast.

The LLM connection comes from `Settings().llm`. The endpoint, model and context window can be overridden with `LNVOX_LLM__ENDPOINT` / `LNVOX_LLM_ENDPOINT`, `LNVOX_LLM_MODEL` and `LNVOX_LLM_MAX_MODEL_LEN` (see [environment](/reference/environment/)).

## In the launcher

```bash title="scripts/run_pipeline.sh"
    if [ "$MODE" = "narration" ]; then
        # This volume's cast only: s1 deliberately doesn't merge prior
        # volumes' casts (s1_characters.merge_clusters) — series continuity
        # is voice cast's job (PriorCastIndex matches recurring characters).
        banner "Stage 1: cast extraction (this volume; series voices carry over at voice cast)"
        run_step ensure_vllm "Stage 1 (cast extraction)" uv run lnvox s1 "$BOOK_ID"
```

- It runs only when `--mode narration` (the default) is set, right after ingest. It is skipped for `--mode lecture`, `--mode scenario` and `--skip-llm`.
- `run_step ensure_vllm …` gives the step up to `--step-retries` attempts (default 3), 5 s apart by default (`$RETRY_DELAY`). A signal exit (Ctrl-C) aborts at once, and so does exit 2 (a deterministic refusal). Before each retry it calls `ensure_vllm`, which relaunches the managed LLM server if it stopped answering `/v1/models`. With `--vllm-url` it is a no-op. The per-chapter cache means a retry resumes at the chapter that failed. See [launcher functions](/launcher/functions/).
- The launcher always runs s1 with no `--chapters`, so every narration run rebuilds `01_characters.json` from the per-chapter cache and one fresh merge call.

## Inputs and outputs

| Path under `artifacts/<book_id>/` | Direction | Contents |
| --- | --- | --- |
| `00_text.jsonl` | read | Chapters from [ingest](/stages/ingest/) |
| `../<earlier-volume>/01_characters.json` | read | Prior volumes' casts (found by `find_prior_volumes`). Loaded but **not** sent to the LLM |
| `01_characters_per_chapter/<chapter_id>.json` | read/write | Raw per-chapter extraction, the resume cache |
| `01_characters.json` | write | Final cast (`CharacterList`) |
| `01_characters_merge_log.json` | write | Cluster provenance. Not written for a one-chapter book with no prior volumes |

JSON examples of all three outputs are on the [library page](/library/stages/s1-characters/#artifacts).

## How it works

`cli.stage1` → [`s1_characters.run`](/library/stages/s1-characters/#run) → [`extract_per_chapter`](/library/stages/s1-characters/#extract_per_chapter) (per chapter, cached) → [`sanitize_character_list`](/library/stages/s1-characters/#sanitize_character_list) → [`cluster_characters`](/library/stages/s1-characters/#cluster_characters) → [`merge_clusters`](/library/stages/s1-characters/#merge_clusters) → [`_apply_merge_groups`](/library/stages/s1-characters/#_apply_merge_groups).

1. **Extract per chapter.** One LLM call per chapter (`characters_per_chapter.jinja`) returns `name`, `aliases`, `gender`, `approx_age`, `description` and `evidence` for every character who appears, speaks or acts. The output budget is up to 24K tokens. A runaway, truncated response is salvaged entry by entry.
2. **Sanitise.** It drops names that never occur in the chapter (the model copying its own few-shot "Sir Patrick"), drops pronoun names (`I`, `She`), and renames `The Narrator` to the exact `Narrator` the voice matcher expects.
3. **Cluster deterministically.** A union-find fuses entries that share an *identity-safe* name/alias key: one whose carriers are spelling variants of one name. It also fuses near-identical names (difflib ratio ≥ 0.9, at least 4 characters, equal enumerators: digit runs and a trailing single letter or roman numeral, so `Attendant A` / `Attendant B` stay two people). Generic shared tokens such as "attendant", "the merchant" or "milady" never link people and are stripped from aliases, as are all-lowercase aliases.
4. **Merge groups (LLM).** One call sees a compact row per cluster and returns only the groups of differently-named clusters that are the same person. The code applies those groups to its own clusters and refuses any group that would put two known genders into one character, counting every member already merged by earlier groups. If the payload is over 160 000 characters, or the model's answer is unusable, the deterministic clusters are used unchanged. A dead server fails the step instead.
5. **Write** the cast (most chapters first) and the merge log.

Cross-volume continuity is **not** handled here. A recurring character keeps their voice because [voice cast](/stages/voice-cast/) reuses prior-volume clip assignments by name and alias (DESIGN.md §2.2, §6.4).

## Debugging a bad cast

| Symptom | Likely cause | Where to look / fix |
| --- | --- | --- |
| Two people fused into one | Same exact name. A shared alias between names where one contains the other (at least 4 characters: `Anna` / `Annabelle`). Or an LLM merge group | `merged_from` in `01_characters_merge_log.json` shows deterministic fusions. If that cluster isn't fused but the final cast is, the LLM grouped them |
| One person appears twice | No shared key, and a name ratio under 0.9 (`Tuuli` / `Tuli` = 0.889), or differing enumerators. The LLM didn't group them | Add the other name as an alias in one `01_characters_per_chapter/<id>.json` entry and re-run s1 |
| Character missing | Filtered by attestation: no word-bounded match of the name or any alias in that chapter's text | Check the chapter text for the spelling the model used |
| `Narrator` in the cast | The extractor labelled an unnamed first-person POV "The Narrator" | Intended. That character is voiced by the narrator clip |
| Role words missing from aliases | By design: all-lowercase and shared generic aliases are dropped | Put them in `description` instead. Casting and speaker tags don't need them |

:::caution[Hand edits to 01_characters.json don't survive]
Every launcher run re-runs s1, which rewrites `01_characters.json`. Put durable fixes in the per-chapter cache files. Sanitisation and clustering re-run on them without any new extraction calls. Or run the stages by hand, skipping s1.
:::

## Re-running and caching

- **Per-chapter results are cached** in `01_characters_per_chapter/<chapter_id>.json`, stored raw. A file that fails to parse is re-extracted. Delete a file to force re-extraction of that chapter.
- Sanitisation, clustering and the merge LLM call are **not** cached. They run again every time. The merge call can group slightly differently between runs.
- Downstream caches don't notice a changed cast. `02_scenes/` (s2) and `03_directed/` (s3) keep their per-chapter files. Delete them to propagate a cast change.

## Failure modes

- **LLM server down or crashing mid-stage.** Per-chapter extraction exceptions propagate. The step fails, `run_step` runs `ensure_vllm`, and the retry resumes from the cache.
- **Truncated JSON** (`finish_reason=length`). `LLMClient.structured` grows the budget 1.5 times per attempt, up to the context ceiling, then falls back to salvage. A `[salvage] recovered N character(s)` line means the model looped. Duplicates are harmless: they cluster away.
- **Merge answer unusable** (bad JSON after the client's retries). It fails silently. The cast is the deterministic clusters, and the merge log is still written. Differently-named duplicates, like a character known by a title early and a name later, then stay separate. A transport error in the merge call (server unreachable, timeout, 5xx) is re-raised instead, so an un-merged cast is never written because the server died.
- **Context window smaller than 65 536 tokens.** The client clamps output budgets against its context window: `LNVOX_LLM__MAX_MODEL_LEN` / `LNVOX_LLM_MAX_MODEL_LEN`, falling back to `LNVOX_LLM_MAX_LEN`, default 65 536. The launcher's `--max-model-len` exports all three, and a launcher-started `serve_vllm.sh` for a 27B/31B model passes its 20 480 default on to the client the same way. A window chosen inside a serve script you started yourself, or on an external `--vllm-url` server, never reaches the client: export `LNVOX_LLM_MAX_LEN` for both, or pass `--max-model-len`. Otherwise the 24K-token extraction request can exceed the context and return a 400. See [context length](/reference/environment/#llm-context-length-three-names).
