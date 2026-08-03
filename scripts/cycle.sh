#!/usr/bin/env bash
# One sourcing + apply cycle: source both markets, drive DE and India candidates
# to COVERED independently, then submit whatever the safety caps allow.
#
# Run on a schedule (e.g. every 2h):   scripts/cycle.sh
# Override targets:                    DE_TARGET=10 IN_TARGET=5 scripts/cycle.sh
#
# Apply-stage safety is untouched: the first 3 submissions of each run are dry,
# same-domain submits are spaced >=90s, and the daily cap (25, hard ceiling 50)
# flips everything to dry-run once reached. A cycle that hits the cap still
# sources and prepares candidates — they just queue for the next day.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

DE_TARGET=${DE_TARGET:-10}
IN_TARGET=${IN_TARGET:-10}
# Candidates are pulled well above target because the fit-score gate (min 6) is
# the real bottleneck, not the apply caps. Measured on the 2026-07-30 DE run:
# 60 candidates scored -> 56 below threshold, 4 COVERED (~7% yield). At 15x a
# target of 10 means ~150 scored, so a cycle can run well past 2h; launchd will
# not start a second copy while one is still going.
POOL_FACTOR=${POOL_FACTOR:-15}

echo "==================== cycle start: $(date '+%Y-%m-%d %H:%M:%S') ===================="

echo ""
echo "-------- [1/6] discover --------"
honestapply discover

# Cheap no-LLM gate: drops dead hosts (Indeed expires, LinkedIn is ban-guarded)
# and plainly non-engineering titles before any LLM call is spent on them.
echo ""
echo "-------- [2/6] prefilter --------"
honestapply prefilter

drive() {
  local country=$1 target=$2
  shift 2
  echo ""
  echo "-------- drive ${country} (target ${target}) $* --------"
  local ids
  ids=$(python scripts/pick_candidates.py --country "$country" --limit $((target * POOL_FACTOR)) "$@")
  if [ -z "$ids" ]; then
    echo "no eligible ${country} candidates"
    return
  fi
  python scripts/batch_drive.py --ids "$ids" --target "$target"
}

echo ""
echo "-------- [3/6] prepare Germany --------"
drive DE "$DE_TARGET"

echo ""
echo "-------- [4/6] prepare India --------"
drive IN "$IN_TARGET"

# Last line of defence before anything reaches an employer: inspect the rendered
# PDFs themselves and route any letter containing model commentary (or a
# suspiciously short body) to needs_human instead of submitting it.
echo ""
echo "-------- [5/6] verify letters --------"
python scripts/verify_letters.py

echo ""
echo "-------- [6/6] apply (caps enforced) --------"
honestapply apply --no-dry-run

echo ""
echo "-------- status --------"
honestapply status

echo ""
echo "==================== cycle done: $(date '+%Y-%m-%d %H:%M:%S') ===================="
