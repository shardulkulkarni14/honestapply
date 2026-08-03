#!/usr/bin/env bash
# Germany, established employers only (no early-stage startups).
# Size has no field in the data, so it is inferred two ways in
# pick_candidates.py --established-only:
#   * the employer matches config/big_medium_employers.txt, or
#   * the employer has >= --min-postings roles on file (a startup posts a few,
#     an established company posts dozens).
# Staffing agencies and placeholder company names are excluded outright.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate
DE_TARGET=${DE_TARGET:-15}

echo "=========== german established $(date '+%F %T') ==========="
echo "--- prepare DE (established only, target ${DE_TARGET}) ---"
DE=$(python scripts/pick_candidates.py --country DE --limit $((DE_TARGET*12)) --established-only 2>/dev/null)
if [ -z "$DE" ]; then echo "no eligible candidates"; exit 0; fi
python scripts/batch_drive.py --ids "$DE" --target "$DE_TARGET"

echo "--- verify letters ---"
python scripts/verify_letters.py

echo "--- apply ---"
honestapply apply --no-dry-run 2>&1 | tail -50

echo "=========== done — $(sqlite3 data/honestapply.db "select count(*) from applications where mode='real' and status='applied' and applied_at >= datetime('now','-24 hours');")/100 real in 24h ==========="
