#!/usr/bin/env bash
# Source fresh, then prepare and submit India candidates only.
set -uo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate
IN_TARGET=${IN_TARGET:-15}

echo "=========== india run $(date '+%F %T') ==========="
echo "--- discover (492 boards, 190 searches) ---"
honestapply discover 2>&1 | tail -1
echo "--- prefilter ---"
honestapply prefilter 2>&1 | tail -1

python - <<'PY'
import sqlite3, importlib
pc = importlib.import_module('scripts.pick_candidates')
con=sqlite3.connect('data/honestapply.db')
rows=con.execute("select title,location,url from jobs where status in ('discovered','enriched','scored','tailored')").fetchall()
from collections import Counter
c=Counter(); rem=0; hyd=0
for t,l,u in rows:
    if any(m in (u or '').lower() for m in pc.DEAD_HOST_MARKERS): continue
    if pc._IN.search(l or ''):
        c[pc._title_rank(t or '')]+=1
        if pc._IN_REMOTE.search(l or ''): rem+=1
        if pc._IN_PRIORITY.search(l or ''): hyd+=1
print(f"INDIA POOL AFTER SOURCING -> tier2={c[2]} tier1={c[1]} | remote={rem} hyderabad={hyd}")
PY

echo "--- prepare India (target ${IN_TARGET}) ---"
IN=$(python scripts/pick_candidates.py --country IN --limit $((IN_TARGET*12)) 2>/dev/null)
[ -n "$IN" ] && python scripts/batch_drive.py --ids "$IN" --target "$IN_TARGET"

echo "--- verify letters ---"
python scripts/verify_letters.py

echo "--- apply ---"
honestapply apply --no-dry-run 2>&1 | tail -40

echo "=========== india run done — $(sqlite3 data/honestapply.db "select count(*) from applications where mode='real' and status='applied' and applied_at >= datetime('now','-24 hours');")/100 real in 24h ==========="
