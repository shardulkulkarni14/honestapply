#!/usr/bin/env bash
# Keep the Germany-only producer + consumer alive until today's real-apply goal
# is met. Restarts either loop if it dies, and logs progress every few minutes.
# Does NOT stop on its own at the goal — it keeps sourcing/applying and just
# notes when the goal is reached, per "if goal doesn't fit, source more, don't stop".
set -uo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate 2>/dev/null

GOAL=${GOAL:-10}
LOG=data/logs/goal_watchdog.log
today_count() {
  sqlite3 data/honestapply.db \
    "select count(*) from applications where mode='real' and status='applied' and date(applied_at)=date('now');" 2>/dev/null || echo 0
}

echo "=== goal watchdog started $(date '+%F %T') | goal=${GOAL} today ===" >> "$LOG"
while true; do
  # Keep the Germany-only producer alive.
  if ! pgrep -f "scripts/overnight.sh" >/dev/null; then
    echo "$(date '+%F %T') producer down -> restarting (DE-only, workers=1)" >> "$LOG"
    PREPARE_WORKERS=1 IN_TARGET=0 nohup bash scripts/overnight.sh > data/logs/producer_latest.log 2>&1 &
  fi
  # Keep the consumer alive.
  if ! pgrep -f "scripts/apply_consumer.sh" >/dev/null; then
    echo "$(date '+%F %T') consumer down -> restarting" >> "$LOG"
    nohup bash scripts/apply_consumer.sh > data/logs/consumer_latest.log 2>&1 &
  fi
  n=$(today_count)
  echo "$(date '+%F %T') applied today: ${n}/${GOAL}" >> "$LOG"
  if [ "$n" -ge "$GOAL" ]; then
    echo "$(date '+%F %T') GOAL REACHED (${n}/${GOAL}) — loops stay running for more" >> "$LOG"
  fi
  sleep 300
done
