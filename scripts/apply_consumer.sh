#!/usr/bin/env bash
# Continuously submit COVERED jobs as they appear, instead of waiting for the
# whole prepare stage to finish.
#
# The prepare stages are LLM-bound and the apply stage is browser-bound, so they
# use different resources and can overlap. Previously apply only started after
# every candidate had been prepared, which left the browser idle for the entire
# (much longer) prepare phase.
#
# Exactly ONE apply process runs at a time — the >=90s same-domain rate limit is
# tracked in-process, and most postings share job-boards.greenhouse.io, so
# concurrent apply processes would hammer one host with no spacing.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

# Each `honestapply apply` invocation re-pays the dry-run canary, so wait for a
# small batch rather than firing per job.
MIN_BATCH=${MIN_BATCH:-3}
POLL_SECONDS=${POLL_SECONDS:-30}
NOTES=data/logs/AUTONOMOUS_NOTES.md

covered_count() {
  sqlite3 data/honestapply.db "select count(*) from jobs where status='covered';" 2>/dev/null || echo 0
}
prepare_running() {
  pgrep -f "batch_drive.py" >/dev/null && return 0 || return 1
}
real_today() {
  sqlite3 data/honestapply.db \
    "select count(*) from applications where mode='real' and status='applied' and applied_at >= datetime('now','-24 hours');" 2>/dev/null || echo 0
}

idle=0
echo "=========== apply consumer started $(date '+%F %T') ==========="

while true; do
  # Never run two apply processes at once.
  if pgrep -f "honestapply apply" >/dev/null; then
    sleep "$POLL_SECONDS"; continue
  fi

  # If the daily cap is reached the apply stage forces everything to dry-run, so
  # continuing would burn browser time producing nothing. Idle until the rolling
  # 24h window frees slots again.
  cap=$(python -c "from honestapply.config import get_settings; print(get_settings().effective_daily_cap)" 2>/dev/null || echo 100)
  if [ "$(real_today)" -ge "$cap" ]; then
    echo "daily cap reached ($(real_today)/${cap}) — idling 15m at $(date '+%H:%M:%S')"
    sleep 900
    continue
  fi

  n=$(covered_count)

  if [ "$n" -ge "$MIN_BATCH" ] || { [ "$n" -gt 0 ] && ! prepare_running; }; then
    echo ""
    echo "--- applying batch of ${n} at $(date '+%H:%M:%S') ---"
    honestapply apply --no-dry-run 2>&1 | tail -25
    echo "--- batch done: $(real_today) real submissions in the last 24h ---"
    continue
  fi

  # Idle, but do NOT exit. This is meant to run unattended for hours alongside a
  # producer that alternates between discovering, prefiltering and preparing —
  # during which there are long stretches with an empty queue and no
  # batch_drive process visible. An earlier version exited after one such gap,
  # two minutes into an overnight run, leaving the queue unattended.
  # It stops only when the operator kills it.
  if [ "$n" -eq 0 ]; then
    idle=$((idle + 1))
    if [ $((idle % 20)) -eq 1 ]; then
      echo "queue empty — waiting for the producer ($(real_today) real in 24h) at $(date '+%H:%M:%S')"
    fi
    sleep "$POLL_SECONDS"
    continue
  fi

  sleep "$POLL_SECONDS"
done
