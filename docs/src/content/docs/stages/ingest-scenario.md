---
title: Stage 0 — ingest-scenario
description: lnvox ingest-scenario structures a theater-script markdown file into 00_script.json, 00_text.jsonl and 01_characters.json, with verbatim-checked dialogue.
sidebar:
  order: 3
  label: 0 · ingest-scenario
---

`lnvox ingest-scenario` is Stage 0 for scenario mode (DESIGN.md §17). It reads a theater script exported to markdown. Scene and sequence headers are detected deterministically; an LLM then classifies every line of each scene as `dialogue`, `staging` or `cue`, and the code rejects any dialogue the LLM did not copy verbatim. The command also extracts the script's own cast list when it has one and prepares the casting roster. Its outputs replace what `ingest` + `s1` produce in narration mode: `00_text.jsonl` (one chapter per scene) and `01_characters.json`. It adds `00_script.json`, the structured script that the [`scenario`](/stages/scenario/) and [`scenario-sync`](/stages/scenario-sync/) stages read.

It **needs a running LLM server**: structuring is an LLM pass.

## Command

```bash
uv run lnvox ingest-scenario MD_PATH --id <id> [--title <title>] [--language fr] [--no-cache]
```

| Argument / option | Default | Meaning |
|---|---|---|
| `MD_PATH` | required | Theater-script markdown file. It must exist and not be a directory |
| `--id` | required | Artifact id (e.g. `my-play`). Output goes to `artifacts/<id>/` |
| `--title` | file name | Play title stored in `00_script.json`. The default is the markdown-cleaned file stem |
| `--language` | `fr` | Language tag stored in `00_script.json`. It is informational only: the voicebank language comes from `LNVOX_VOICEBANK` |
| `--no-cache` | off | Re-roll the structuring: the `cache/scenario/` LLM cache (keyed on schema, system prompt, rendered prompt and model) is not read, and the fresh results **overwrite** its entries, so the next cached run reuses the re-roll |

When it finishes it prints `Done. N scene(s), M item(s) → artifacts/<id>/00_script.json` and the next steps: `lnvox voice cast <id>`, then `lnvox scenario <id>`.

## In the launcher

```bash title="scripts/run_pipeline.sh"
if [ "$MODE" = "scenario" ]; then
    # ingest-scenario is itself an LLM pass (structure + characters);
    # skipped when no file was passed — argument validation above only
    # lets that through when a complete prior ingest exists.
    if [ -n "$SCENARIO_FILE" ]; then
        banner "Stage 0: ingest-scenario (structure + characters)"
        run_step ensure_vllm "Stage 0 (ingest-scenario)" uv run lnvox ingest-scenario "$SCENARIO_FILE" --id "$BOOK_ID" \
                ${TITLE_GIVEN:+--title} ${TITLE_GIVEN:+"$TITLE_GIVEN"}
    else
        banner "Stage 0: ingest-scenario skipped ($BOOK_ART/00_script.json + 01_characters.json exist)"
    fi
```

`$BOOK_ART` is `${LNVOX_ARTIFACTS_DIR:-artifacts}/<book_id>`, the directory the CLI writes to (as long as the variable is exported: the launcher doesn't read `.env`).

- It runs only with `--mode scenario` and `--scenario-file <path>`. The launcher passes `--book-title` on as `--title` when you give one (`TITLE_GIVEN` is `--book-title` before it defaults to the book id), so the play and the `.m4b` share a title. Without it, the title comes from the file name. It passes no `--language` or `--no-cache`, so the language is `fr`.
- **Without `--scenario-file`**, Stage 0 is skipped and the existing ingest is reused, but only a **complete** one: both `00_script.json` and `01_characters.json` must exist under `$BOOK_ART` (`00_text.jsonl` is reused alongside). Argument parsing refuses to start (usage, exit 2) otherwise:

  ```bash title="scripts/run_pipeline.sh"
  if [ "$MODE" = "scenario" ] && [ -z "$SCENARIO_FILE" ] \
      && { [ ! -f "$BOOK_ART/00_script.json" ] || [ ! -f "$BOOK_ART/01_characters.json" ]; }; then
      if [ -f "$BOOK_ART/00_script.json" ]; then
          echo "ERROR: the prior ingest at $BOOK_ART is incomplete (00_script.json but no 01_characters.json — its characters pass never finished)." >&2
          echo "  Re-run with --scenario-file <script.md>; the structuring calls already made are served from cache/scenario/." >&2
      else
          echo "ERROR: scenario mode needs --scenario-file <script.md> (or a prior ingest at $BOOK_ART/: 00_script.json + 01_characters.json)" >&2
      fi
      usage 2
  fi
  ```

  The check follows `LNVOX_ARTIFACTS_DIR` when it is exported (the launcher doesn't read `.env`). The current `ingest-scenario` writes `00_script.json` last, so a crash never leaves it without a cast; the "incomplete" branch catches a folder from an older run, or one whose `01_characters.json` was deleted by hand.
- The skip decision checks only whether the files exist, not whether they are current. After **editing the script**, pass `--scenario-file` again. The content cache means only the changed scenes cost LLM calls.
- `run_step ensure_vllm`: the step is attempted up to `--step-retries` times in total (default 3). Before each retry, the launcher relaunches its LLM server if it stopped responding. With an external `--vllm-url` the hook does nothing.
- It runs in the LLM phase, after the server starts and before `voice cast` and `scenario`. `--skip-llm` skips it together with the rest of that phase. The novels-folder check does not apply in scenario mode.

## Inputs and outputs

| | Path | Notes |
|---|---|---|
| reads | `MD_PATH` | UTF-8 markdown (typically a docx → markdown export) |
| reads/writes | `cache/scenario/<16-hex>.json` | one file per LLM call (written even with `--no-cache`). The path is relative to the working directory |
| writes | `artifacts/<id>/00_script.json` | `ScenarioScript`: `scenario_id`, `title`, `language`, `roster[]`, `scenes[{scene_id, title, items[{type, speaker, text}]}]`. Written **last**, atomically. With `01_characters.json` it is the launcher's "ingest done" marker |
| writes | `artifacts/<id>/00_text.jsonl` | one chapter per scene (`chapter_id` = `scene_id`, title or `Scène NN`, dialogue + staging text), used by s5 for chapter markers |
| writes | `artifacts/<id>/01_characters.json` | `CharacterList` of every speaking, non-group role, ready for `voice cast` |

A minimal `00_script.json` scene (invented content):

```json
{
  "scene_id": "01",
  "title": "Séquence 1 – La porte",
  "items": [
    {"type": "dialogue", "speaker": "Gardien", "text": "Qui va là ?"},
    {"type": "staging", "speaker": "", "text": "Il lève sa lanterne."},
    {"type": "cue", "speaker": "", "text": "SON 3"},
    {"type": "dialogue", "speaker": "Voyageuse", "text": "C'est moi, ouvrez."}
  ]
}
```

Full examples of all three files are on [`lnvox.ingest.scenario`](/library/ingest/scenario/#outputs).

## How it works

`cli.ingest_scenario` → [`scenario.run`](/library/ingest/scenario/#run) → [`chunk_script`](/library/ingest/scenario/#chunk_script) → [`extract_roster`](/library/ingest/scenario/#extract_roster) → per scene [`split_chunk`](/library/ingest/scenario/#split_chunk) → [`structure_chunk`](/library/ingest/scenario/#structure_chunk) → [`validate_items`](/library/ingest/scenario/#validate_items) → [`canonicalize_speakers`](/library/ingest/scenario/#canonicalize_speakers) → [`extract_characters`](/library/ingest/scenario/#extract_characters). Every LLM call goes through [`cached_structured`](/library/ingest/scenario/#cached_structured).

1. **Chunk.** Scene headers (`Séquence 1 – …`, `### 2. TITRE`, `**3**`, `ACTE II`, `3 – TITRE`, …) and cast-list headings (`Personnages`, `Characters`, `Distribution`, …) split the file. Text before the first header is dropped. A file with no scene header becomes one untitled scene holding everything except its cast list (the play after a `Personnages` heading is not swallowed by the roster).
2. **Roster.** Each cast-list section goes to one LLM call that returns `{name, description, actor}` per role.
3. **Structure.** Each scene (split at blank lines when over 7,000 characters) goes to one LLM call that returns ordered items with speaker labels, verbatim text, and stage directions split out of dialogue lines.
4. **Validate.** Each dialogue item must be a substring of its source chunk after markdown unescaping, whitespace collapsing and lowercasing. Failures are kept as `staging` with an empty speaker and counted in a `[warn]` line.
5. **Canonicalize.** Speaker-label variants (`Rôle 1 (Prénom)`, `Rôle 1(Prénom)`, `Rôle 1`) are merged into the most frequent full form.
6. **Characters.** Speaking roles, excluding group labels such as `Tous` or `All`, are sent with their roster bio and two sample lines to one LLM call that fills gender, age and a voice description. The names always come from the script.
7. **Write**, only now that every LLM pass has succeeded: `00_text.jsonl`, then `01_characters.json`, then `00_script.json` (the last two atomically). A crash in any LLM pass leaves the previous run's files untouched and consistent.

Typical log:

```text
Structuring my-play.md → artifacts/my-play/
  12 scene(s), 1 roster section(s) detected
  roster: 9 entrie(s)
  ✓ scene 01: 38 item(s), 27 dialogue line(s) (cached)
  ✓ scene 02: 51 item(s), 40 dialogue line(s)
  ...
  [warn] 2 dialogue line(s) failed verbatim validation and were kept as staging — review 00_script.json
  ✓ 8 character(s) for casting
  LLM cache: 11/14 call(s) served from cache/scenario/
```

## Re-running and caching

- Every LLM call is cached in `cache/scenario/`, keyed on SHA-256 of *schema name + system prompt + rendered prompt + model name*. Re-running after editing one scene re-pays only that scene, plus the characters call if the speaker list or the sampled lines changed. A crash mid-run resumes from the cached scenes.
- Changing a prompt template, the system prompt (`STRUCTURE_SYSTEM`) or `LNVOX_LLM_MODEL` re-keys everything. Swapping model weights under the same name does **not**; delete `cache/scenario/` (or re-roll with `--no-cache`) in that case. Entries cached before the system prompt joined the key miss once and are re-paid.
- `--no-cache` re-rolls every call and overwrites the cached entries, so the next cached run returns the re-rolled structuring.
- Verbatim validation and canonicalization run on every run, cached or not.

## Failure modes

| Symptom | Cause / fix |
|---|---|
| Connection refused / timeout | No LLM server. Start one, or run through the launcher |
| `ValueError: LLM returned content that failed schema validation for SceneStructure …` | The model kept emitting invalid JSON for one chunk. Other scenes are cached. Retry, or use a stronger model |
| `1 scene(s) … detected` for a multi-scene play | Headers not recognised. Compare them with the [header patterns](/library/ingest/scenario/#is_scene_header) |
| Cast-list entries show up as staging in the only scene | No recognised scene headers, and the cast list is blank-line-separated paragraphs rather than a markdown list, so only its first block is taken as roster. See [what gets lost](/library/ingest/scenario/#chunk_script) |
| `[warn] N dialogue line(s) failed verbatim validation` | The LLM paraphrased or merged lines. Search `00_script.json` for `staging` items that are really speech. Fix them by hand, or re-roll with `--no-cache` |
| Two roles merged into one character | Their labels differ only in a parenthetical (`Garde (Paul)` / `Garde (Marc)`) |
| A re-run crashed and the old outputs are still there | Expected: nothing is written until every LLM pass succeeds. Re-run with `--scenario-file`; finished scenes come from cache |
| Launcher: `the prior ingest at … is incomplete (00_script.json but no 01_characters.json …)` (exit 2) | Launched without `--scenario-file` on a folder that has a script but no cast. Re-run with `--scenario-file`; cached calls cost nothing |

:::caution[Lines the LLM skips are not detected]
Validation rejects dialogue that is *not in* the source, but nothing checks that every source line made it *into* the output. For a production script, diff the dialogue in `00_script.json` against the original once.
:::
