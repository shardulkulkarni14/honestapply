"""Ad-hoc batch driver: push ONLY a curated set of job IDs through
enrich -> score -> tailor -> cover, using the per-job stage functions.

Unlike `honestapply run`, this never touches the rest of the backlog: it operates
strictly on the IDs passed in. Each job advances as far as it can; a job that
fails to score the gate, or routes to needs_human, simply stops.

The work happens in honestapply.stages.prepare.drive_jobs, on a small thread
pool (--workers; default HONESTAPPLY_PREPARE_WORKERS, which ships at 1 so the
behaviour is the familiar serial one until raised — 3 is the recommended
value). Keep this file's name and its per-job output lines as they are: the
shell loops pgrep `batch_drive.py` and grep `COVERED (score`.

Usage:
    python scripts/batch_drive.py --ids 101,102,103 [--target 20] [--min-score 6] [--workers 3]
    python scripts/batch_drive.py --ids-file data/batch_ids.txt
"""
from __future__ import annotations

import argparse

from honestapply.config import get_settings
from honestapply.stages.prepare import drive_jobs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="", help="comma-separated job IDs")
    ap.add_argument("--ids-file", default="", help="file with one ID per line")
    ap.add_argument("--target", type=int, default=0, help="stop once this many COVERED reached (0=all)")
    ap.add_argument("--min-score", type=int, default=None)
    ap.add_argument(
        "--workers", type=int, default=0,
        help="jobs prepared concurrently (0 = HONESTAPPLY_PREPARE_WORKERS from settings)",
    )
    args = ap.parse_args()

    ids: list[int] = []
    if args.ids:
        ids += [int(x) for x in args.ids.split(",") if x.strip()]
    if args.ids_file:
        with open(args.ids_file) as fh:
            ids += [int(line.strip()) for line in fh if line.strip() and not line.startswith("#")]
    ids = list(dict.fromkeys(ids))  # de-dup, preserve order
    if not ids:
        raise SystemExit("no IDs provided")

    settings = get_settings()
    threshold = args.min_score if args.min_score is not None else settings.honestapply_min_score
    workers = args.workers if args.workers > 0 else settings.honestapply_prepare_workers
    print(
        f"batch_drive: {len(ids)} jobs, threshold={threshold}, "
        f"target={args.target or 'all'}, workers={workers}",
        flush=True,
    )

    summary = drive_jobs(
        ids,
        workers=workers,
        target=args.target,
        min_score=threshold,
        emit=lambda line: print(line, flush=True),
    )

    print(f"\nbatch_drive done: {summary.covered} newly COVERED", flush=True)


if __name__ == "__main__":
    main()
