#!/usr/bin/env bash
# Continuous Germany producer: source and prepare candidates from ESTABLISHED
# employers only (no early-stage startups), leaving apply to the consumer.
#
# Runs alongside scripts/apply_consumer.sh — preparing is LLM-bound and applying
# is browser-bound, so they overlap instead of taking turns. Exactly one apply
# process ever runs; that is the consumer's job, not this script's.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

DE_TARGET=${DE_TARGET:-12}
POOL_FACTOR=${POOL_FACTOR:-12}
MAX_QUEUE=${MAX_QUEUE:-20}      # pause producing if the consumer falls behind
NOTES=data/logs/AUTONOMOUS_NOTES.md

covered_count() {
  sqlite3 data/honestapply.db "select count(*) from jobs where status in ('covered','dry_run_completed');" 2>/dev/null || echo 0
}
real_today() {
  sqlite3 data/honestapply.db \
    "select count(*) from applications where mode='real' and status='applied' and applied_at >= datetime('now','-24 hours');" 2>/dev/null || echo 0
}

round=0
while true; do
  round=$((round + 1))
  echo ""
  echo "=========== DE producer round ${round} — $(date '+%F %T') ==========="

  q=$(covered_count)
  if [ "$q" -ge "$MAX_QUEUE" ]; then
    echo "queue at ${q} (>= ${MAX_QUEUE}) — pausing 10m for the consumer"
    sleep 600
    continue
  fi

  echo "--- discover ---"
  honestapply discover 2>&1 | tail -1
  echo "--- prefilter ---"
  honestapply prefilter 2>&1 | tail -1

  echo "--- prepare DE (established only, target ${DE_TARGET}) ---"
  ids=$(python scripts/pick_candidates.py --country DE \
          --limit $((DE_TARGET * POOL_FACTOR)) --established-only 2>/dev/null)
  if [ -z "$ids" ]; then
    echo "no eligible established DE candidates — sleeping 20m"
    sleep 1200
    continue
  fi
  python scripts/batch_drive.py --ids "$ids" --target "$DE_TARGET" \
    > data/logs/prepare_de_established.log 2>&1

  n=$(grep -c 'COVERED (score' data/logs/prepare_de_established.log 2>/dev/null | head -1)
  echo "round ${round}: +${n} covered | queue $(covered_count) | $(real_today) real in 24h"
  echo "- $(date '+%Y-%m-%d %H:%M')  DE producer round ${round}: +${n} covered; $(real_today) real in 24h" >> "$NOTES"

  # Nothing new means the established-employer pool is momentarily dry; wait for
  # employers to post rather than re-scoring the same rejected candidates.
  if [ "$n" -eq 0 ]; then
    echo "no new candidates — sleeping 20m"
    sleep 1200
  fi
done
