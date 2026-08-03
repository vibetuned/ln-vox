"""Staged s4 driver — DESIGN.md §15.3/§15.4.

Builds the plan, then runs the four GPU phases in order, each as a
subprocess (`lnvox s4-phase …`) so every attempt gets a fresh CUDA
context and a crash can't poison the next phase. A failed phase is
simply relaunched: completed items skip by file existence, so each
restart costs one single-model load instead of the full four-model
TTSServer boot.

Crash policy (per user decision, §15.4): NO item is ever skipped —
crashes on non-ECC hardware are transient and the same item almost
always renders on the next attempt. attempts.json is diagnostics only.
The one guard is the stall limit: if LNVOX_S4_STALL_LIMIT (default 10)
consecutive restarts complete zero new items, we abort loudly naming
the stuck item so a human can look at it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from lnvox.tts.staged import (
    PHASES,
    build_plan,
    count_pending,
    first_pending,
    staged_root,
    write_plan,
)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_attempt(root: Path, phase: str, rc: int, progress: Callable[[str], None]) -> None:
    inflight = _read_json(root / "inflight.json")
    item = inflight.get("item", "<unknown>")
    attempts_path = root / "attempts.json"
    attempts = _read_json(attempts_path)
    key = f"{phase}:{item}"
    attempts[key] = attempts.get(key, 0) + 1
    tmp = attempts_path.with_name(attempts_path.name + ".tmp")
    tmp.write_text(json.dumps(attempts, indent=2), encoding="utf-8")
    os.replace(tmp, attempts_path)
    progress(
        f"  phase '{phase}' exited rc={rc} while on item {item} "
        f"(attempt {attempts[key]} for this item) — relaunching"
    )


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
) -> None:
    root = staged_root(book_dir)
    for sub in ("refs", "ctx", "latents"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    # The plan is derived state — rebuild every run (cheap, deterministic).
    # Intermediates are content-keyed, so a rebuilt plan re-uses everything
    # already on disk. attempts.json persists across runs as diagnostics.
    plan = build_plan(
        book_id,
        chapters,
        casting,
        voicebank,
        voicebank_root,
        cache_dir,
        limit=limit,
        device=device,
    )
    write_plan(root, plan)
    cached = sum(1 for r in plan.renders if r.cached)
    total_chunks = sum(len(r.chunks) for r in plan.renders)
    progress(
        f"Staged s4 plan: {len(plan.beats)} beats → {len(plan.renders)} unique renders "
        f"({cached} already cached), {total_chunks} denoise chunks, "
        f"{len(plan.refs)} voice refs"
    )

    stall_limit = int(os.environ.get("LNVOX_S4_STALL_LIMIT", "10"))

    for phase in PHASES:
        attempt = 0
        stall = 0
        while True:
            before = count_pending(phase, plan, root, cache_dir)
            # decode always runs at least once: it also places beat WAVs
            # from the cache and writes the per-chapter manifests.
            if before == 0 and (phase != "decode" or attempt > 0):
                break
            attempt += 1
            # No square brackets here — cli routes progress through
            # rich.console, which would eat them as markup tags.
            progress(f"phase {phase}: {before} pending (attempt {attempt})")
            rc = subprocess.call(
                [
                    sys.executable,
                    "-m",
                    "lnvox.cli",
                    "s4-phase",
                    phase,
                    book_id,
                    "--device",
                    device,
                ]
            )
            after = count_pending(phase, plan, root, cache_dir)
            if rc == 0 and after == 0:
                break
            _record_attempt(root, phase, rc, progress)
            if after < before:
                stall = 0
            else:
                stall += 1
                if stall >= stall_limit:
                    stuck = first_pending(phase, plan, root, cache_dir) or "<unknown>"
                    raise RuntimeError(
                        f"Staged s4 phase '{phase}' made no progress across "
                        f"{stall_limit} consecutive restarts; stuck on item "
                        f"{stuck}. No item is ever skipped (DESIGN.md §15.4) — "
                        f"investigate this item, then re-run `lnvox s4 {book_id} "
                        f"--staged` to resume. Raise LNVOX_S4_STALL_LIMIT to keep "
                        f"grinding through transient faults."
                    )
        progress(f"phase {phase}: complete")

    attempts = _read_json(root / "attempts.json")
    if attempts:
        worst = sorted(attempts.items(), key=lambda kv: -kv[1])[:10]
        progress(
            "Crash diagnostics (item: restarts attributed): "
            + ", ".join(f"{k}={v}" for k, v in worst)
        )

    if keep_staged:
        progress(f"Keeping staged intermediates at {root} (--keep-staged)")
    else:
        shutil.rmtree(root, ignore_errors=True)
