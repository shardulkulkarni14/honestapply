#!/usr/bin/env bash
# Continuous overnight producer: keep sourcing and preparing candidates in a
# loop, so the apply consumer always has work and never idles.
#
# Pairs with scripts/apply_consumer.sh, which runs alongside and submits jobs as
# they reach COVERED. Producer and consumer are separate processes on purpose:
# preparing is LLM-bound and applying is browser-bound, so they overlap cleanly.
#
# Safety is unchanged and still enforced in code: daily cap, >=90s same-domain
# spacing, the dry-run canary, dedup, and the verify gate on every letter.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

DE_TARGET=${DE_TARGET:-12}     # Germany + rest of Europe
IN_TARGET=${IN_TARGET:-15}     # India (Hyderabad first, then remote-into-India)
POOL_FACTOR=${POOL_FACTOR:-12}
MAX_QUEUE=${MAX_QUEUE:-25}     # stop preparing if the consumer is this far behind
NOTES=data/logs/AUTONOMOUS_NOTES.md

covered_count() {
  sqlite3 data/honestapply.db "select count(*) from jobs where status='covered';" 2>/dev/null || echo 0
}
real_today() {
  sqlite3 data/honestapply.db \
    "select count(*) from applications where mode='real' and status='applied' and applied_at >= datetime('now','-24 hours');" 2>/dev/null || echo 0
}

round=0
while true; do
  round=$((round + 1))
  echo ""
  echo "=========== producer round ${round} — $(date '+%F %T') ==========="

  # Don't run the queue away from the consumer; applying is the slower half.
  q=$(covered_count)
  if [ "$q" -ge "$MAX_QUEUE" ]; then
    echo "queue at ${q} (>= ${MAX_QUEUE}) — pausing production 10m so the consumer catches up"
    sleep 600
    continue
  fi

  echo "--- discover ---"
  honestapply discover 2>&1 | tail -1

  echo "--- prefilter ---"
  honestapply prefilter 2>&1 | tail -1

  echo "--- prepare EU + IN (parallel) ---"
  prepare() {
    local country=$1 target=$2 log=$3
    local ids
    ids=$(python scripts/pick_candidates.py --country "$country" --limit $((target * POOL_FACTOR)) 2>/dev/null)
    if [ -z "$ids" ]; then echo "no eligible ${country} candidates" > "$log"; return; fi
    python scripts/batch_drive.py --ids "$ids" --target "$target" > "$log" 2>&1
  }
  prepare EU "$DE_TARGET" data/logs/prepare_eu.log &
  P1=$!
  prepare IN "$IN_TARGET" data/logs/prepare_in.log &
  P2=$!
  wait $P1; wait $P2

  eu=$(grep -c 'COVERED (score' data/logs/prepare_eu.log 2>/dev/null | head -1)
  inn=$(grep -c 'COVERED (score' data/logs/prepare_in.log 2>/dev/null | head -1)
  echo "round ${round}: EU +${eu}, IN +${inn} covered | queue $(covered_count) | $(real_today) real in 24h"
  echo "- $(date '+%Y-%m-%d %H:%M')  producer round ${round}: EU +${eu}, IN +${inn} covered; $(real_today) real submissions in 24h" >> "$NOTES"

  # If a whole round produced nothing, the pools are momentarily dry — wait for
  # employers to post rather than spinning on the same exhausted candidates.
  if [ "$eu" -eq 0 ] && [ "$inn" -eq 0 ]; then
    echo "no new candidates this round — sleeping 20m"
    sleep 1200
  fi
done
