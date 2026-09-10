#!/usr/bin/env bash
# One-shot PREPARE run for a Germany + rest-of-Europe wave.
# Sources are already discovered; this only drives candidates to COVERED
# (enrich -> score -> tailor -> cover). It deliberately does NOT apply — the
# apply stage is run separately so there is a human checkpoint on the covered
# queue before any real submission.
#
#   DE_TARGET   covered German roles to prepare      (default 18)
#   EU_TARGET   covered non-German Europe roles      (default 33)
# Targets sit a little above the 15 / 30 ask to absorb apply-stage attrition
# (login walls and captchas route otherwise-good roles to needs_human).
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate

DE_TARGET=${DE_TARGET:-18}
EU_TARGET=${EU_TARGET:-33}
DE_POOL=${DE_POOL:-160}
EU_POOL=${EU_POOL:-320}

covered() { sqlite3 data/honestapply.db "select count(*) from jobs where status='covered';" 2>/dev/null || echo 0; }

echo "=========== DE+EU prepare — $(date '+%F %T') ==========="
echo "start covered: $(covered)"

echo "--- pick DE (pool ${DE_POOL}, target ${DE_TARGET}) ---"
DE_IDS=$(python scripts/pick_candidates.py --country DE --limit "${DE_POOL}" 2>/dev/null)
if [ -n "$DE_IDS" ]; then
  echo "DE candidates: $(echo "$DE_IDS" | tr ',' '\n' | wc -l | tr -d ' ')"
  python scripts/batch_drive.py --ids "$DE_IDS" --target "$DE_TARGET"
else
  echo "no DE candidates picked"
fi
echo "covered after DE: $(covered)"

echo "--- pick EU_OTHER (pool ${EU_POOL}, target ${EU_TARGET}) ---"
EU_IDS=$(python scripts/pick_candidates.py --country EU_OTHER --limit "${EU_POOL}" 2>/dev/null)
if [ -n "$EU_IDS" ]; then
  echo "EU candidates: $(echo "$EU_IDS" | tr ',' '\n' | wc -l | tr -d ' ')"
  python scripts/batch_drive.py --ids "$EU_IDS" --target "$EU_TARGET"
else
  echo "no EU candidates picked"
fi
echo "covered after EU: $(covered)"

echo "--- verify letters ---"
python scripts/verify_letters.py 2>&1 | tail -5 || true

echo "=========== prepare done — $(date '+%F %T') | covered=$(covered) ==========="
