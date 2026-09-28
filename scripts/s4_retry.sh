#!/usr/bin/env bash
# Auto-restart `lnvox s4` until it exits successfully or hits MAX_ATTEMPTS.
# Cached beats from prior attempts skip immediately, so each restart resumes
# from where the previous one died.
#
# Usage:
#     [MAX_ATTEMPTS=N] ./scripts/s4_retry.sh <book_id>
#
# Exit status: 0 once an attempt succeeds; 2 at once when `lnvox s4` exits 2
# (a deterministic usage/config refusal — retrying would replay it
# identically, so there's no point burning the budget); 1 after
# MAX_ATTEMPTS failed attempts. Every other non-zero exit — a Python
# exception (1), a crash or kill by signal (>= 128, which `uv run` passes
# through: segfault, OOM kill) — is retried. RETRY_DELAY (seconds, default
# 5) spaces the attempts, as it does for the launcher's other steps.
#
# The log goes to $LNVOX_ARTIFACTS_DIR/<book>/_s4_full.log (default
# artifacts/) — next to the rest of the book's artifacts, wherever
# `lnvox s4` itself writes them.
set -uo pipefail

if [ $# -lt 1 ] || [ -z "$1" ]; then
    echo "Usage: [MAX_ATTEMPTS=N] $0 <book_id>" >&2
    exit 2
fi
BOOK="$1"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-100}"
RETRY_DELAY="${RETRY_DELAY:-5}"
BOOK_ART="${LNVOX_ARTIFACTS_DIR:-artifacts}/$BOOK"
LOG="$BOOK_ART/_s4_full.log"
mkdir -p "$BOOK_ART"

attempt=0
while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    {
        echo ""
        echo "==================== Attempt $attempt @ $(date -Iseconds) ===================="
    } | tee -a "$LOG"

    # lnvox's own status, read from PIPESTATUS straight after the pipeline.
    # Branching on the pipeline instead (`if uv run … | tee …; then`) only
    # ever reached the then-branch with exit 0 — under pipefail every failure
    # takes the else — so the per-exit-code handling was dead code.
    uv run lnvox s4 "$BOOK" 2>&1 | tee -a "$LOG"
    ec="${PIPESTATUS[0]}"
    if [ "$ec" -eq 0 ]; then
        echo "==================== Completed on attempt $attempt ====================" | tee -a "$LOG"
        exit 0
    elif [ "$ec" -eq 2 ]; then
        echo "==================== Exit 2 on attempt $attempt: deterministic refusal, not retrying ====================" | tee -a "$LOG"
        exit 2
    elif [ "$ec" -ge 128 ]; then
        echo "==================== Crashed on attempt $attempt; retrying in ${RETRY_DELAY}s ====================" | tee -a "$LOG"
    else
        echo "==================== Exit $ec on attempt $attempt; retrying in ${RETRY_DELAY}s ====================" | tee -a "$LOG"
    fi
    sleep "$RETRY_DELAY"
done

echo "==================== Gave up after $MAX_ATTEMPTS attempts ====================" | tee -a "$LOG"
exit 1
