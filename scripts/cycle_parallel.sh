#!/usr/bin/env bash
# One cycle with the two prepare stages running CONCURRENTLY, then a single
# sequential apply.
#
#   discover -> prefilter -> [ prepare DE || prepare IN ] -> verify -> apply
#
# Why prepare is parallel but apply is not:
#   * prepare is LLM-bound (~70s/candidate) and is the bulk of cycle time, so
#     running Germany and India together roughly halves it. Safe since the DB is
#     now WAL with a 30s busy timeout (see db/session.py).
#   * apply must stay sequential. The >=90s same-domain rate limit lives in the
#     apply process's memory, so two apply processes cannot see each other's
#     timing — and most postings share job-boards.greenhouse.io, so running them
#     in parallel would hammer one host with no spacing and risk being flagged as
#     bot traffic. It also shares one browser profile.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

DE_TARGET=${DE_TARGET:-10}
IN_TARGET=${IN_TARGET:-10}
POOL_FACTOR=${POOL_FACTOR:-15}
NOTES=data/logs/AUTONOMOUS_NOTES.md
note() { echo "- $(date '+%Y-%m-%d %H:%M')  $*" >> "$NOTES"; }

echo "==================== cycle start: $(date '+%F %T') ===================="

echo ""
echo "-------- [1/5] discover --------"
honestapply discover 2>&1 | tail -2

echo ""
echo "-------- [2/5] prefilter --------"
honestapply prefilter 2>&1 | tail -1

echo ""
echo "-------- [3/5] prepare DE + IN (parallel) --------"
prepare() {
  local country=$1 target=$2 log=$3
  local ids
  ids=$(python scripts/pick_candidates.py --country "$country" --limit $((target * POOL_FACTOR)) 2>/dev/null)
  if [ -z "$ids" ]; then
    echo "no eligible ${country} candidates" > "$log"
    return
  fi
  python scripts/batch_drive.py --ids "$ids" --target "$target" > "$log" 2>&1
}

prepare DE "$DE_TARGET" data/logs/prepare_de.log &
PID_DE=$!
prepare IN "$IN_TARGET" data/logs/prepare_in.log &
PID_IN=$!
wait $PID_DE; wait $PID_IN

echo "DE: $(grep -c 'COVERED (score' data/logs/prepare_de.log 2>/dev/null || echo 0) covered"
echo "IN: $(grep -c 'COVERED (score' data/logs/prepare_in.log 2>/dev/null || echo 0) covered"

echo ""
echo "-------- [4/5] verify letters --------"
python scripts/verify_letters.py

echo ""
echo "-------- [5/5] apply (sequential; caps + rate limit enforced) --------"
honestapply apply --no-dry-run 2>&1 | tail -60

REAL=$(sqlite3 data/honestapply.db "select count(*) from applications where mode='real' and status='applied' and applied_at >= datetime('now','-24 hours');")
note "cycle_parallel done: ${REAL} real submissions in the last 24h"
echo ""
echo "==================== cycle done: $(date '+%F %T') — ${REAL} real in 24h ===================="
