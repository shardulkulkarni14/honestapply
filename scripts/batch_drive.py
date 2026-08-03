"""Ad-hoc batch driver: push ONLY a curated set of job IDs through
enrich -> score -> tailor -> cover, using the per-job stage functions.

Unlike `honestapply run`, this never touches the rest of the backlog: it operates
strictly on the IDs passed in. Each job advances as far as it can; a job that
fails to score the gate, or routes to needs_human, simply stops.

Usage:
    python scripts/batch_drive.py --ids 101,102,103 [--target 20] [--min-score 6]
    python scripts/batch_drive.py --ids-file data/batch_ids.txt
"""
from __future__ import annotations

import argparse

from honestapply.config import PATHS, get_settings, load_profile
from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.llm.base import get_provider
from honestapply.resume.schema import list_resumes
from honestapply.stages.enrich import _enrich_job
from honestapply.stages.score import _score_one
from honestapply.stages.tailor import _tailor_one
from honestapply.stages.cover_letter import _cover_one


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="", help="comma-separated job IDs")
    ap.add_argument("--ids-file", default="", help="file with one ID per line")
    ap.add_argument("--target", type=int, default=0, help="stop once this many COVERED reached (0=all)")
    ap.add_argument("--min-score", type=int, default=None)
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
    provider = get_provider(settings)
    profile = load_profile()
    profile_summary = profile.summary_for_scoring()
    resumes = list_resumes(PATHS.resumes_dir)

    covered = 0
    print(f"batch_drive: {len(ids)} jobs, threshold={threshold}, target={args.target or 'all'}")

    for jid in ids:
        if args.target and covered >= args.target:
            print(f"target {args.target} COVERED reached — stopping")
            break

        # Each job in its own transaction so one failure can't roll back others.
        try:
            with session_scope() as s:
                job = s.get(Job, jid)
                if job is None:
                    print(f"[{jid}] not found")
                    continue
                tag = f"[{jid}] {job.company} / {(job.title or '')[:45]}"

                if job.status == Status.DISCOVERED:
                    ok = _enrich_job(job)
                    if not ok or job.status != Status.ENRICHED:
                        print(f"{tag} -> enrich failed ({job.status})")
                        continue

                if job.status == Status.ENRICHED:
                    _score_one(job, provider, profile_summary, threshold)
                    if job.status != Status.SCORED:
                        print(f"{tag} -> score={job.score} {job.status}")
                        continue

                if job.status == Status.SCORED:
                    _tailor_one(job, provider, resumes)
                    if job.status != Status.TAILORED:
                        print(f"{tag} -> tailor: {job.status} ({job.status_reason})")
                        continue

                if job.status == Status.TAILORED:
                    _cover_one(job, provider, resumes)

                if job.status == Status.COVERED:
                    covered += 1
                    print(f"{tag} -> COVERED (score={job.score})  [{covered} total]")
                else:
                    print(f"{tag} -> {job.status} ({job.status_reason})")
        except Exception as exc:  # noqa: BLE001
            print(f"[{jid}] EXCEPTION: {exc}")

    print(f"\nbatch_drive done: {covered} newly COVERED")


if __name__ == "__main__":
    main()
