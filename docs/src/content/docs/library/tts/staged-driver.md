---
title: lnvox.tts.staged_driver
description: run_staged - builds the staged s4 plan and runs each phase as an `lnvox s4-phase` subprocess with resume, a stall limit and crash diagnostics.
sidebar:
  order: 4
  label: staged_driver
---

`lnvox.tts.staged_driver` orchestrates the staged Stage 4 render (DESIGN.md
§15.3 and §15.4). `lnvox s4 --staged` calls [`run_staged`](#run_staged), which
builds the plan in-process. It then runs the four
[phases](/library/tts/staged/#phases) in order, each as its own
`lnvox s4-phase` subprocess, and relaunches a phase until nothing is pending.
Each restart only pays the cost of reloading one model, not the full
four-model `TTSServer` boot. The subprocess boundary also means a poisoned CUDA
context cannot leak into the next attempt.

## At a glance

| Name | Purpose |
|---|---|
| `run_staged(...)` | Plan, run phases with retries, print diagnostics, clean up. |
| `_record_attempt(root, phase, rc, progress)` | Increment `attempts.json["<phase>:<item>"]` after a failed attempt. |
| `_read_json(path)` | Tolerant JSON read that returns `{}` on a missing or corrupt file. |
| `LNVOX_S4_STALL_LIMIT` | Number of consecutive zero-progress restarts before the driver aborts. Default `10`. |

## API

### `run_staged`

```python
def run_staged(
    *,
    book_id: str,
    chapters,
    casting,
    voicebank,
    voicebank_root: Path,
    book_dir: Path,
    cache_dir: Path,
    device: str,
    limit: Optional[int] = None,
    keep_staged: bool = False,
    progress: Callable[[str], None] = print,
) -> None
```

**Parameters**

- `book_id`: book id, passed on to each `lnvox s4-phase` subprocess.
- `chapters`: the `ChapterDirected` list to render, already filtered by
  `--chapters`.
- `casting`, `voicebank`, `voicebank_root`: casting and voicebank used to map
  speakers to reference clips.
- `book_dir`: `artifacts/<book>`. Intermediates go under
  `book_dir/05_audio/_staged/`.
- `cache_dir`: the content cache. `lnvox s4` passes `Path("cache") / "tts"`.
- `device`: torch device, passed to both `build_plan` and every subprocess.
- `limit`: render only the first N beats overall.
- `keep_staged`: keep `_staged/` after a successful run. Default `False`.
- `progress`: logging callback. The CLI routes it through `rich`, so messages
  deliberately avoid square brackets, which rich would treat as markup.

**Flow**

1. Create `_staged/refs`, `_staged/ctx` and `_staged/latents`.
2. Call [`build_plan`](/library/tts/staged/#build_plan) and write
   `plan.json`. The plan is rebuilt on every run because it is cheap and
   deterministic, and the intermediates it points at are content-keyed. It
   reports
   `Staged s4 plan: N beats → M unique renders (K already cached), C denoise chunks, R voice refs`,
   then `Render recipe: <model_version>`, the effective recipe (and cache-key
   tag) for this device.
3. For each phase in `PHASES`, run the retry loop below and then report
   `phase <name>: complete`.
4. If `attempts.json` has entries, print the 10 items with the most restarts:
   `Crash diagnostics (item: restarts attributed): ...`.
5. Unless `keep_staged` is set, delete `_staged/` with
   `shutil.rmtree(root, ignore_errors=True)`.

**Retry loop (per phase)**

```python
while True:
    before = count_pending(phase, plan, root, cache_dir)
    if before == 0 and (phase != "decode" or attempt > 0):
        break                       # decode always runs once: it places WAVs + writes manifests
                                    # (its count includes pending placements/manifests)
    attempt += 1
    rc = subprocess.call([sys.executable, "-m", "lnvox.cli", "s4-phase",
                          phase, book_id, "--device", device])
    after = count_pending(phase, plan, root, cache_dir)
    if rc == 0 and after == 0:
        break
    _record_attempt(root, phase, rc, progress)   # "phase 'X' exited rc=… while on item …"
    stall = 0 if after < before else stall + 1
    if stall >= stall_limit:
        raise RuntimeError(...)     # names first_pending(...) as the stuck item
```

That is a simplified copy of `src/lnvox/tts/staged_driver.py`.

- **Decode is done only when its outputs are.** `count_pending("decode")`
  counts renders missing from the cache **plus**
  [`pending_placements`](/library/tts/staged/#pending-work-calculators): placed
  WAVs that are not exact copies of their cache entries, and chapter manifests
  that are missing or do not match the plan. A decode subprocess that writes
  every cache WAV and then dies inside `_finalize` (for example on a full disk)
  is therefore re-run, and the stall guard names the stuck placement as
  `placement <chapter>/<file>` if it keeps failing.
- **No item is ever skipped** (DESIGN.md §15.4). An audiobook with a silently
  missing beat is worse than a stalled run. Crashes on non-ECC consumer GPUs are
  transient, and the same item almost always renders on the next attempt.
- **The stall guard.** When `LNVOX_S4_STALL_LIMIT` consecutive restarts of one
  phase complete zero new items, the driver raises a `RuntimeError`. The error
  names the stuck item and tells you to investigate, re-run
  `lnvox s4 <book> --staged` to resume, or raise the limit. Any progress resets
  the counter, so a run with many scattered crashes keeps going.
- **Subprocess environment.** Each phase inherits the parent's working
  directory and environment. That is how `LNVOX_S4_NO_TRIM` (set by
  `--no-trim`), the `LNVOX_S4_*` recipe knobs and `LNVOX_ARTIFACTS_DIR` reach
  the phases. The phase always uses `cache/tts` relative to the working
  directory.

**Raises**: `RuntimeError` from the stall guard. Errors from `build_plan` (for
example, cast clip ids missing from the voicebank) propagate before any GPU
work starts.

**Reads / Writes**: everything under `artifacts/<book>/05_audio/_staged/`,
`cache/tts/<key>.wav`, `artifacts/<book>/05_audio/<chapter>/`. See the
[storage layout](/library/tts/staged/#_staged-storage-layout).

**Env vars**: `LNVOX_S4_STALL_LIMIT` (int, default `10`).

### `_record_attempt`

```python
def _record_attempt(root: Path, phase: str, rc: int, progress: Callable[[str], None]) -> None
```

Reads the item from `inflight.json`, falling back to `<unknown>`. It increments
`attempts.json["<phase>:<item>"]`, writing via `.tmp` and `os.replace`, and
reports the failure. Counts are diagnostics only. `attempts.json` survives a
failed or aborted run, so it accumulates across resumes, and it is deleted with
the rest of `_staged/` after a successful run unless `--keep-staged` is set.

## Resume semantics

Nothing needs to be passed to resume a run. Run the same `lnvox s4 <book> --staged`
command again:

- Refs, ctx and latents already on disk are reused because their names are
  content-keyed.
- Renders whose `cache/tts/<key>.wav` exists are marked `cached` and skipped
  entirely.
- An interrupted **monolithic** run can be picked up by the staged path (and
  the other way round) only when the effective
  [variant tags](/library/tts/dramabox-client/#s4_variant_tag) match. With the
  current defaults they do not, so switching paths mid-book re-renders every
  beat.

## Notes

- The launcher runs the staged path **without** an outer retry loop.
  `run_pipeline.sh` calls `uv run lnvox s4 "$BOOK_ID" --staged` directly and
  exits 1 if it fails, so a stall abort stops the pipeline immediately.
  `scripts/s4_retry.sh` is only used for the monolithic and VibeVoice paths.
  See [Stage 4](/stages/s4-tts/#in-the-launcher).
- `--keep-staged` is a debugging aid. After a successful run, `_staged/` still
  holds `plan.json`, `inflight.json`, `refs/` and `ctx/neg.safetensors`. Decode
  has already deleted each finished render's ctx and latent files. The kept
  `neg.safetensors` is how F10's all-NaN encoder output was found. To inspect a
  beat's own ctx or latent tensors, stop the run before its decode completes.
  Their files stay on disk until the render is decoded.
