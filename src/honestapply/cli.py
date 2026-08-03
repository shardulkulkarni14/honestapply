"""honestapply CLI (Typer).

Stage commands lazy-import their implementation modules so the CLI stays usable
even while individual stages are still being built. The functions each stage
module must expose are documented inline next to the command.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from honestapply import __version__
from honestapply.config import PATHS, get_settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Autonomous job-application pipeline: discover → enrich → score → tailor → cover-letter → apply.",
)
console = Console()


# ---------------------------------------------------------------------------
# version / status / init / doctor
# ---------------------------------------------------------------------------
@app.command()
def version() -> None:
    """Print the honestapply version."""
    console.print(f"honestapply {__version__}")


@app.command()
def init() -> None:
    """Interactive setup: create .env and example config files if missing."""
    root = PATHS.root
    created: list[str] = []

    env = root / ".env"
    if not env.exists() and (root / ".env.example").exists():
        env.write_text((root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
        created.append(".env (from .env.example — fill in your API key)")

    # Copy example configs to their live names if not present.
    pairs = [
        ("profile.example.json", "profile.json"),
        ("searches.example.yaml", "searches.yaml"),
        ("answers.example.yaml", "answers.yaml"),
        ("employers.example.yaml", "employers.yaml"),
    ]
    for example, live in pairs:
        ex, lv = PATHS.config_dir / example, PATHS.config_dir / live
        if ex.exists() and not lv.exists():
            lv.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(f"config/{live}")

    # The résumé is seeded too, not just mentioned in the closing hint. Without a
    # résumé YAML the tailor stage has nothing to work from, so `honestapply
    # simulate` — the offline "does this work at all" check — stops at `scored`
    # on a fresh clone. Seeding the example makes the first run complete.
    resume_ex = PATHS.config_dir / "resume.example.yaml"
    resume_live = PATHS.resumes_dir / "default.yaml"
    if resume_ex.exists() and not resume_live.exists():
        resume_live.parent.mkdir(parents=True, exist_ok=True)
        resume_live.write_text(resume_ex.read_text(encoding="utf-8"), encoding="utf-8")
        created.append(f"{resume_live} (example facts — replace with YOUR OWN)")

    from honestapply.db.session import init_db

    init_db()
    created.append(f"database at {get_settings().db_path}")

    if created:
        console.print("[green]Initialized:[/green]")
        for c in created:
            console.print(f"  • {c}")
    else:
        console.print("[yellow]Nothing to do — already initialized.[/yellow]")
    console.print(
        "\nNext: edit [cyan]config/profile.json[/cyan] and "
        "[cyan]data/resumes/default.yaml[/cyan], replacing every value with YOUR "
        "facts — the seeded files contain example data, and the tailor validator "
        "will faithfully carry those examples into a real application if you "
        "leave them. Then run [cyan]honestapply doctor[/cyan]."
    )


@app.command()
def doctor() -> None:
    """Check the environment: Python, deps, API keys, Playwright MCP, Chrome, DB."""
    settings = get_settings()
    table = Table(title="honestapply doctor", show_lines=False)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    def row(name: str, ok: bool | None, detail: str = "") -> None:
        mark = "[green]✓[/green]" if ok else ("[red]✗[/red]" if ok is False else "[yellow]–[/yellow]")
        table.add_row(name, mark, detail)

    # Python
    v = sys.version_info
    row("Python ≥ 3.11", v >= (3, 11), f"{v.major}.{v.minor}.{v.micro}")

    # Core + optional deps
    def importable(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except Exception:
            return False

    for label, mod, required in [
        ("SQLAlchemy", "sqlalchemy", True),
        ("Typer", "typer", True),
        ("Pydantic", "pydantic", True),
        ("Jinja2 (resume)", "jinja2", False),
        ("WeasyPrint (resume)", "weasyprint", False),
        ("python-jobspy (discover)", "jobspy", False),
        ("Streamlit (dashboard)", "streamlit", False),
        ("anthropic SDK (llm)", "anthropic", False),
    ]:
        ok = importable(mod)
        row(label, ok if (required or ok) else None,
            "installed" if ok else ("MISSING (required)" if required else "optional — not installed"))

    # LLM provider readiness
    provider = settings.llm_provider
    if provider == "claude_cli":
        has_cli = bool(shutil.which("claude"))
        row("LLM (claude_cli)", has_cli,
            "uses Claude Code login — no API key needed" if has_cli else "claude CLI not on PATH")
    elif provider == "stub":
        row("LLM (stub)", None, "offline stub — no key needed")
    else:
        key = settings.api_key_for()
        row(f"LLM key ({provider})", bool(key),
            "present" if key else "missing — set in .env (or use LLM_PROVIDER=claude_cli for no key)")

    # Playwright MCP
    claude_bin = shutil.which("claude")
    mcp_ok = None
    detail = "claude CLI not found"
    if claude_bin:
        try:
            out = subprocess.run(
                [claude_bin, "mcp", "list"], capture_output=True, text=True, timeout=30
            ).stdout.lower()
            mcp_ok = "playwright" in out
            detail = "configured" if mcp_ok else "not configured — run: claude mcp add playwright -- npx @playwright/mcp@latest"
        except Exception as exc:  # pragma: no cover
            mcp_ok = False
            detail = f"check failed: {exc}"
    row("Playwright MCP", mcp_ok, detail)

    # Chrome / Chromium
    chrome_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    chrome = next((p for p in chrome_paths if Path(p).exists()), shutil.which("google-chrome"))
    row("Chrome present", bool(chrome), chrome or "not found (Playwright can install one)")

    # DB writable
    try:
        from honestapply.db.session import init_db

        init_db()
        row("Database writable", True, str(settings.db_path))
    except Exception as exc:
        row("Database writable", False, str(exc))

    console.print(table)


@app.command()
def status() -> None:
    """Show counts per status, recent applications, and success rate."""
    from honestapply.stages.track import summarize

    s = summarize()
    table = Table(title="Pipeline status")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for st, count in sorted(s.counts.items()):
        table.add_row(st, str(count))
    table.add_row("[bold]TOTAL[/bold]", f"[bold]{s.total}[/bold]")
    console.print(table)
    console.print(
        f"Real submissions: {s.real_submissions}  |  Dry runs: {s.dry_runs}  |  "
        f"Success rate: {s.success_rate:.0%}"
    )
    if s.recent_applications:
        rt = Table(title="Recent applications")
        for col in ("job_id", "applied_at", "mode", "status", "confirmation"):
            rt.add_column(col)
        for a in s.recent_applications:
            rt.add_row(*(str(a[c]) for c in ("job_id", "applied_at", "mode", "status", "confirmation")))
        console.print(rt)


@app.command()
def mark(
    job_id: int = typer.Argument(...),
    new_status: str = typer.Argument(..., help="applied | needs_human | failed | ..."),
) -> None:
    """Manually override a job's status."""
    from honestapply.db.models import Job, Status
    from honestapply.db.session import session_scope

    if new_status not in Status.ALL:
        console.print(f"[red]Unknown status.[/red] Valid: {', '.join(Status.ALL)}")
        raise typer.Exit(1)
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job:
            console.print(f"[red]No job {job_id}.[/red]")
            raise typer.Exit(1)
        job.status = new_status
        console.print(f"[green]Job {job_id} → {new_status}[/green]")


# ---------------------------------------------------------------------------
# Pipeline stages (lazy imports; each module exposes the named run_* function)
# ---------------------------------------------------------------------------
@app.command()
def discover(source: str = typer.Option(None, help="Limit to one source/board.")) -> None:
    """Stage 1 — discover jobs from boards + company ATS pages. → run_discover()"""
    from honestapply.stages.discover import run_discover

    n = run_discover(sources=[source] if source else None)
    console.print(f"[green]Discovered {n} new jobs.[/green]")


def _parse_ids(ids: str | None) -> list[int] | None:
    """Parse a comma/space-separated --ids string into a list of ints (or None)."""
    if not ids:
        return None
    out = [int(x) for x in ids.replace(",", " ").split() if x.strip()]
    return out or None


@app.command()
def prefilter(
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit to."),
    limit: int = typer.Option(None),
) -> None:
    """Stage 1.5 — cheap no-LLM relevance gate; routes obvious non-fits to skipped. → run_prefilter()"""
    from honestapply.stages.prefilter import run_prefilter

    r = run_prefilter(ids=_parse_ids(ids), limit=limit)
    console.print(
        f"[green]Prefilter: scanned {r['scanned']}, dropped {r['dropped']} junk, "
        f"kept {r['kept']} for the LLM stages.[/green]"
    )


@app.command()
def enrich(
    limit: int = typer.Option(None),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit to."),
) -> None:
    """Stage 2 — fetch full descriptions. → run_enrich()"""
    from honestapply.stages.enrich import run_enrich

    console.print(f"[green]Enriched {run_enrich(limit=limit, ids=_parse_ids(ids))} jobs.[/green]")


@app.command()
def score(
    min_score: int = typer.Option(None, help="Override min fit score."),
    limit: int = typer.Option(None),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit to."),
) -> None:
    """Stage 3 — LLM fit scoring + gate. → run_score()"""
    from honestapply.stages.score import run_score

    console.print(f"[green]Scored {run_score(min_score=min_score, limit=limit, ids=_parse_ids(ids))} jobs.[/green]")


@app.command()
def tailor(
    limit: int = typer.Option(None),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit to."),
) -> None:
    """Stage 4 — tailor resume per job (immutable-facts enforced). → run_tailor()"""
    from honestapply.stages.tailor import run_tailor

    console.print(f"[green]Tailored {run_tailor(limit=limit, ids=_parse_ids(ids))} resumes.[/green]")


@app.command(name="cover-letter")
def cover_letter(
    limit: int = typer.Option(None),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit to."),
) -> None:
    """Stage 5 — generate per-job cover letters. → run_cover_letters()"""
    from honestapply.stages.cover_letter import run_cover_letters

    console.print(f"[green]Generated {run_cover_letters(limit=limit, ids=_parse_ids(ids))} cover letters.[/green]")


@app.command()
def apply(
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run"),
    no_safety: bool = typer.Option(False, "--no-safety", help="Disable the first-3-dry-runs guard."),
    limit: int = typer.Option(None),
    url: str = typer.Option(None, help="Mode B: just emit the apply instructions for this URL."),
    enable_linkedin_easy_apply: bool = typer.Option(False, "--enable-linkedin-easy-apply"),
) -> None:
    """Stage 6 — fill/submit applications via Claude Code + Playwright MCP. → run_apply()"""
    from honestapply.stages.apply import run_apply

    run_apply(
        dry_run=dry_run,
        no_safety=no_safety,
        limit=limit,
        url=url,
        enable_linkedin_easy_apply=enable_linkedin_easy_apply,
    )


@app.command(name="docs-bundle")
def docs_bundle(
    out: str = typer.Option("data/outputs/transcripts_and_references.pdf", help="Output PDF path."),
) -> None:
    """Merge profile.supporting_documents (transcripts, references, Arbeitszeugnis) into one PDF."""
    from honestapply.documents import build_bundle, supporting_document_paths

    srcs = supporting_document_paths()
    if not srcs:
        console.print("[yellow]No supporting_documents found in profile (or files missing).[/yellow]")
        raise typer.Exit(1)
    result = build_bundle(out)
    if result:
        console.print(f"[green]Merged {len(srcs)} document(s) → {result}[/green]")
    else:
        console.print("[red]Could not build bundle (no usable PDFs).[/red]")
        raise typer.Exit(1)


@app.command(name="apply-packet")
def apply_packet(
    ids: str = typer.Option(None, help="Comma-separated COVERED job IDs (default: all COVERED)."),
    limit: int = typer.Option(None),
) -> None:
    """Build manual-assist apply packets for captcha/login-walled forms (no auto-submit)."""
    from honestapply.stages.manual_assist import run_manual_assist

    n = run_manual_assist(ids=_parse_ids(ids), limit=limit)
    console.print(f"[green]Built {n} manual-assist packet(s) → data/outputs/<id>/apply_packet.md[/green]")


@app.command()
def run(
    stages: list[str] = typer.Argument(None, help="Stages to run; default = all up to ready_to_apply."),
    prefilter: bool = typer.Option(False, "--prefilter", help="Run the cheap relevance gate before enrich."),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit every stage to."),
    limit: int = typer.Option(None, help="Cap how many jobs each LLM stage processes."),
) -> None:
    """Chain stages. Default runs discover→enrich→score→tailor→cover-letter (NOT apply).

    Use --prefilter to drop obvious non-fits cheaply first, and --ids/--limit to
    drive only a curated/bounded subset instead of the whole backlog.
    """
    id_list = _parse_ids(ids)
    order = ["discover", "enrich", "score", "tailor", "cover-letter"]
    todo = stages or order
    if prefilter and "prefilter" not in todo:
        # insert right after discover (or at the front if discover isn't running)
        pos = todo.index("discover") + 1 if "discover" in todo else 0
        todo = todo[:pos] + ["prefilter"] + todo[pos:]
    fn = {
        "discover": lambda: __import__("honestapply.stages.discover", fromlist=["run_discover"]).run_discover(),
        "prefilter": lambda: __import__("honestapply.stages.prefilter", fromlist=["run_prefilter"]).run_prefilter(ids=id_list, limit=limit),
        "enrich": lambda: __import__("honestapply.stages.enrich", fromlist=["run_enrich"]).run_enrich(limit=limit, ids=id_list),
        "score": lambda: __import__("honestapply.stages.score", fromlist=["run_score"]).run_score(limit=limit, ids=id_list),
        "tailor": lambda: __import__("honestapply.stages.tailor", fromlist=["run_tailor"]).run_tailor(limit=limit, ids=id_list),
        "cover-letter": lambda: __import__("honestapply.stages.cover_letter", fromlist=["run_cover_letters"]).run_cover_letters(limit=limit, ids=id_list),
    }
    for st in todo:
        if st not in fn:
            console.print(f"[red]Unknown stage: {st}[/red]")
            raise typer.Exit(1)
        console.print(f"[bold cyan]▶ {st}[/bold cyan]")
        fn[st]()


@app.command()
def simulate(jobs: int = typer.Option(5, help="How many fake jobs to run end-to-end.")) -> None:
    """End-to-end dry simulation with fake jobs + stub LLM (no keys/network/browser)."""
    from honestapply.sim import run_simulation

    run_simulation(num_jobs=jobs)


@app.command()
def dashboard(
    port: int = typer.Option(8501, help="Port to serve the dashboard on."),
) -> None:
    """Launch the FastAPI dashboard (one table, all local artifacts linked)."""
    api_path = PATHS.root / "dashboard" / "api.py"
    if not api_path.exists():
        console.print("[red]dashboard/api.py not found.[/red]")
        raise typer.Exit(1)

    import importlib.util

    import uvicorn

    spec = importlib.util.spec_from_file_location("honestapply_dashboard_api", api_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    console.print(f"honestapply dashboard → [cyan]http://localhost:{port}[/cyan]")
    uvicorn.run(module.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
