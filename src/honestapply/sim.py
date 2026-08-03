"""End-to-end simulation: run the whole pipeline offline with fake jobs.

`honestapply simulate` exercises score → tailor → cover-letter → apply(dry-run) using
the StubProvider (no API key) and mock apply (no browser/claude). It writes to a
dedicated sim DB and uses high job IDs (900000+) so it never collides with real
pipeline data or output folders. This is the fastest way to confirm the full
chain works before pointing real API keys + a browser at it.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

_SIM_ID_BASE = 900_000

# A handful of realistic AI/GenAI postings spanning ATS types.
_FAKE_JOBS = [
    {
        "company": "Helix AI",
        "title": "Senior AI Engineer (LLM / RAG)",
        "location": "Munich, Germany",
        "ats": "greenhouse",
        "url": "https://boards.greenhouse.io/helixai/jobs/{n}",
        "description": (
            "We are hiring a Senior AI Engineer to build production agentic systems. "
            "You will work with Python, LangChain, LangGraph, RAG (hybrid search, reranking), "
            "FastAPI, pgvector and Azure. Experience shipping LLM applications to enterprise "
            "customers and owning delivery end-to-end is required."
        ),
    },
    {
        "company": "Northwind Labs",
        "title": "Forward Deployed Engineer, GenAI",
        "location": "Remote (EU)",
        "ats": "lever",
        "url": "https://jobs.lever.co/northwind/{n}",
        "description": (
            "Forward Deployed Engineer to work directly with enterprise customers, scope use "
            "cases, build demos and deploy GenAI solutions. Strong Python, solution architecture, "
            "stakeholder management and technical consulting skills. Customer-facing delivery."
        ),
    },
    {
        "company": "Vega Robotics",
        "title": "Machine Learning Engineer (Computer Vision)",
        "location": "Berlin, Germany",
        "ats": "ashby",
        "url": "https://jobs.ashbyhq.com/vega/{n}",
        "description": (
            "ML Engineer for computer vision in manufacturing. PyTorch, YOLO, model quality "
            "metrics, production monitoring, CI/CD on Azure, Docker, Linux. Optimize inference "
            "pipelines for real-time analytics."
        ),
    },
    {
        "company": "Atlas Fintech",
        "title": "AI Platform Engineer",
        "location": "Berlin, Germany",
        "ats": "greenhouse",
        "url": "https://boards.greenhouse.io/atlasfintech/jobs/{n}",
        "description": (
            "Build the AI platform: multi-tenant LLM services, MCP servers, LangSmith "
            "observability, RBAC and SSO. Stack is Next.js, FastAPI, PostgreSQL/pgvector, Redis. "
            "Production-grade systems and enterprise integration."
        ),
    },
    {
        "company": "Quanta Health",
        "title": "Generative AI Engineer",
        "location": "Remote (Germany)",
        "ats": "ashby",
        "url": "https://jobs.ashbyhq.com/quanta/{n}",
        "description": (
            "Generative AI Engineer for clinical workflows. RAG pipelines, LLM fine-tuning, "
            "agent-based retrieval, prompt engineering with OpenAI and Anthropic models. Python, "
            "FastAPI, evaluation and observability."
        ),
    },
]


def _seed(num_jobs: int) -> list[int]:
    from honestapply.db.models import Job, Status, url_hash
    from honestapply.db.session import session_scope

    ids: list[int] = []
    with session_scope() as s:
        for i in range(num_jobs):
            spec = _FAKE_JOBS[i % len(_FAKE_JOBS)]
            jid = _SIM_ID_BASE + i + 1
            url = spec["url"].format(n=jid)
            s.add(
                Job(
                    id=jid,
                    company=spec["company"] + ("" if i < len(_FAKE_JOBS) else f" {i}"),
                    title=spec["title"],
                    location=spec["location"],
                    url=url,
                    url_hash=url_hash(url),
                    ats_type=spec["ats"],
                    source_board=spec["ats"],
                    status=Status.ENRICHED,
                    description=spec["description"],
                    requirements=spec["description"],
                )
            )
            ids.append(jid)
    return ids


def run_simulation(num_jobs: int = 5) -> None:
    # Force offline, mock, isolated DB. Set BEFORE settings are first read.
    os.environ["LLM_PROVIDER"] = "stub"
    os.environ["HONESTAPPLY_APPLY_MOCK"] = "1"

    from honestapply.config import PATHS, get_settings

    get_settings.cache_clear()  # discard any cached non-stub settings

    sim_db = PATHS.data_dir / "honestapply_sim.db"
    if sim_db.exists():
        sim_db.unlink()

    from honestapply.db.session import init_db

    init_db(sim_db)  # binds the global engine to the sim DB for all stages

    console.rule("[bold cyan]honestapply simulate[/bold cyan]")
    console.print(
        f"Offline end-to-end run: [bold]{num_jobs}[/bold] fake jobs · stub LLM · mock apply\n"
        f"Sim DB: {sim_db}\n"
    )

    ids = _seed(num_jobs)
    console.print(f"Seeded {len(ids)} ENRICHED jobs (ids {ids[0]}–{ids[-1]}).\n")

    from honestapply.stages.cover_letter import run_cover_letters
    from honestapply.stages.score import run_score
    from honestapply.stages.tailor import run_tailor

    steps = [
        ("score", lambda: run_score()),
        ("tailor", lambda: run_tailor()),
        ("cover-letter", lambda: run_cover_letters()),
    ]
    for label, fn in steps:
        with console.status(f"running {label}…"):
            n = fn()
        console.print(f"  [green]✓[/green] {label}: {n}")

    # apply(dry-run): first-3 guard applies, but everything is dry-run anyway.
    from honestapply.stages.apply import run_apply

    with console.status("running apply (dry-run, mock)…"):
        run_apply(dry_run=True, no_safety=False)
    console.print("  [green]✓[/green] apply (dry-run): done\n")

    _summary(ids)


def _summary(ids: list[int]) -> None:
    from honestapply.db.models import Application, Job
    from honestapply.db.session import session_scope

    table = Table(title="Simulation results")
    for col in ("job", "company", "status", "score", "resume", "cover", "apply"):
        table.add_column(col)

    ok_resume = ok_cover = ok_apply = 0
    with session_scope() as s:
        for jid in ids:
            j = s.get(Job, jid)
            if not j:
                continue
            r = bool(j.tailored_resume_path and Path(j.tailored_resume_path).exists())
            c = bool(j.cover_letter_path and Path(j.cover_letter_path).exists())
            app = (
                s.query(Application)
                .filter(Application.job_id == jid)
                .order_by(Application.applied_at.desc())
                .first()
            )
            ok_resume += r
            ok_cover += c
            ok_apply += bool(app)
            table.add_row(
                str(jid),
                (j.company or "")[:18],
                j.status,
                str(j.score),
                "✓" if r else "—",
                "✓" if c else "—",
                (app.status if app else "—"),
            )

    console.print(table)
    total = len(ids)
    console.print(
        f"\n[bold]{ok_resume}/{total}[/bold] resumes · "
        f"[bold]{ok_cover}/{total}[/bold] cover letters · "
        f"[bold]{ok_apply}/{total}[/bold] apply records — all PDFs in data/outputs/<job_id>/"
    )
    console.print(
        "View it: [cyan]HONESTAPPLY_DB_PATH=data/honestapply_sim.db honestapply dashboard[/cyan]\n"
    )
    if ok_resume == total and ok_cover == total and ok_apply == total:
        console.print("[bold green]SIMULATION PASSED — full pipeline works end-to-end.[/bold green]")
    else:
        console.print("[bold yellow]Simulation completed with gaps — see table above.[/bold yellow]")
