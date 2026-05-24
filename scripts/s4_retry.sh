#!/usr/bin/env bash
# Auto-restart `lnvox s4` until it exits successfully or hits MAX_ATTEMPTS.
# Cached beats from prior attempts skip immediately, so each restart resumes
# from where the previous one died.
set -uo pipefail

BOOK="${1:-toaru-volume-1}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"
LOG="artifacts/$BOOK/_s4_full.log"
mkdir -p "artifacts/$BOOK"

attempt=0
while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    {
        echo ""
        echo "==================== Attempt $attempt @ $(date -Iseconds) ===================="
    } | tee -a "$LOG"

    if uv run lnvox s4 "$BOOK" 2>&1 | tee -a "$LOG"; then
        ec="${PIPESTATUS[0]}"
        if [ "$ec" = "0" ]; then
            echo "==================== Completed on attempt $attempt ====================" | tee -a "$LOG"
            exit 0
        else
            echo "==================== Exit $ec on attempt $attempt; retrying in 5s ====================" | tee -a "$LOG"
        fi
    else
        echo "==================== Crashed on attempt $attempt; retrying in 5s ====================" | tee -a "$LOG"
    fi
    sleep 5
done

echo "==================== Gave up after $MAX_ATTEMPTS attempts ====================" | tee -a "$LOG"
exit 1
