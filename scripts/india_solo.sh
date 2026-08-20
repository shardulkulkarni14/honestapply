#!/usr/bin/env bash
# Self-contained India producer: discover -> prefilter -> prepare India roles,
# in a loop, leaving apply to scripts/apply_consumer.sh.
#
# WHY THIS EXISTS (2026-08-13)
#   scripts/india_producer.sh deliberately does NOT discover — it assumes
#   scripts/de_producer.sh is running alongside and owns discovery. When the
#   operator wants India *only*, nothing refreshes the pool and the India
#   producer spins against a stale backlog forever. This script is the India
#   equivalent of de_producer.sh: it owns discovery for an India-only run.
#
#   Do NOT run this at the same time as de_producer.sh or india_producer.sh —
#   two concurrent `honestapply discover` processes just duplicate work and fight
#   over the boards.
#
# SUPPLY REALITY (measured 2026-08-13, worth knowing before you judge the rate)
#   India yield is low and this loop is a slow accumulator, not a burst tool:
#     - prefilter drops every indeed./linkedin. URL by design, and 226 of 227
#       India postings found via job-board searches died on exactly that rule —
#       only postings exposing a direct employer link survive.
#     - The employer-ATS route is near-exhausted: of 120 India board tokens
#       probed, the 8 that resolved were already in employers.yaml.
#     - Of 179 live India candidates scored in one day, 2 hit score 7, 1 hit 6,
#       5 hit 5. Expect roughly 1-3 qualifying roles per full cycle.
#   The 65 India applications on file accumulated over weeks. Let this run for
#   hours/days; do not expect a batch of 30 in one sitting.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

IN_TARGET=${IN_TARGET:-12}
POOL_FACTOR=${POOL_FACTOR:-12}
MAX_QUEUE=${MAX_QUEUE:-24}      # pause producing if the consumer falls behind
NOTES=data/logs/AUTONOMOUS_NOTES.md

covered_count() {
  sqlite3 data/honestapply.db "select count(*) from jobs where status in ('covered','dry_run_completed');" 2>/dev/null || echo 0
}
real_india_total() {
  sqlite3 data/honestapply.db "
    select count(*) from applications a join jobs j on j.id = a.job_id
    where a.mode='real' and a.status='applied'
      and (j.location like '%India%'    or j.location like '%Bengaluru%'
        or j.location like '%Bangalore%' or j.location like '%Mumbai%'
        or j.location like '%Pune%'      or j.location like '%Hyderabad%'
        or j.location like '%Delhi%'     or j.location like '%Gurgaon%'
        or j.location like '%Gurugram%'  or j.location like '%Noida%'
        or j.location like '%Chennai%'   or j.location like '%Karnataka%'
        or j.location like '%Telangana%');" 2>/dev/null || echo 0
}

round=0
echo "=========== india solo producer started $(date '+%F %T') ==========="
echo "India real submissions on file at start: $(real_india_total)"

while true; do
  round=$((round + 1))
  echo ""
  echo "=========== IN solo round ${round} — $(date '+%F %T') ==========="

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

  echo "--- prepare IN (target ${IN_TARGET}) ---"
  ids=$(python scripts/pick_candidates.py --country IN \
          --limit $((IN_TARGET * POOL_FACTOR)) 2>/dev/null)
  if [ -z "$ids" ]; then
    echo "no live India candidates this round — sleeping 20m for new postings"
    sleep 1200
    continue
  fi
  python scripts/batch_drive.py --ids "$ids" --target "$IN_TARGET" \
    > data/logs/prepare_in_solo.log 2>&1

  n=$(grep -c 'COVERED (score' data/logs/prepare_in_solo.log 2>/dev/null | head -1)
  echo "round ${round}: +${n} IN covered | queue $(covered_count) | $(real_india_total) India real total"
  echo "- $(date '+%Y-%m-%d %H:%M')  IN solo round ${round}: +${n} covered; $(real_india_total) India real total" >> "$NOTES"

  # New postings appear on a scale of hours, not minutes. Without this the loop
  # would re-discover the same board contents back-to-back and burn the day's
  # rate-limit budget for nothing.
  echo "--- round ${round} complete, sleeping 15m ---"
  sleep 900
done
