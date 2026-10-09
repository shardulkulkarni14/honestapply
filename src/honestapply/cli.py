"""honestapply CLI (Typer).

Stage commands lazy-import their implementation modules so the CLI stays usable
even while individual stages are still being built. The functions each stage
module must expose are documented inline next to the command.
"""

from __future__ import annotations

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
        ("FastAPI (dashboard)", "fastapi", False),
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

    # MCP servers (Playwright for apply, Gmail for inbox) — one `claude mcp list`.
    claude_bin = shutil.which("claude")
    mcp_list = ""
    mcp_err: str | None = None
    if claude_bin:
        try:
            mcp_list = subprocess.run(
                [claude_bin, "mcp", "list"], capture_output=True, text=True, timeout=30
            ).stdout.lower()
        except Exception as exc:  # pragma: no cover
            mcp_err = str(exc)

    def _mcp_row(label: str, name: str, add_hint: str) -> None:
        if not claude_bin:
            row(label, None, "claude CLI not found")
        elif mcp_err is not None:
            row(label, False, f"check failed: {mcp_err}")
        else:
            present = name in mcp_list
            # `claude mcp list` marks a remote server "connected"/"failed" after
            # its name; treat an explicit "connected" as the healthy signal.
            connected = present and "fail" not in mcp_list.split(name, 1)[1][:40]
            row(label, present and connected, "connected" if present else f"not configured — {add_hint}")

    _mcp_row("Playwright MCP (apply)", "playwright",
             "run: claude mcp add playwright -- npx @playwright/mcp@latest")

    # Gmail inbox: the local MCP server is honestapply-owned, so its health is the
    # local token file, not `claude mcp list` (which reflects the Claude account).
    if settings.gmail_mcp_credentials_file.exists():
        row("Gmail MCP (inbox)", True,
            f"connected — token at {settings.gmail_mcp_credentials_file} (scopes: {settings.gmail_mcp_scopes})")
    elif settings.gmail_mcp_oauth_keys_file.exists():
        row("Gmail MCP (inbox)", None, "client keys set up; not connected — run: honestapply gmail-connect")
    else:
        row("Gmail MCP (inbox)", None, "optional — see docs/GMAIL_SETUP.md to enable")

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

    # Account auto-signup (opt-in): enabled? secret backend usable?
    if getattr(settings, "honestapply_enable_account_signup", False):
        try:
            from honestapply.vault import _keyring_usable, get_vault

            backend = "keyring" if _keyring_usable() else "encrypted file"
            n = len(get_vault().list_sites())
            row("Account signup", True, f"ENABLED · vault: {backend} · {n} saved account(s)")
        except Exception as exc:  # pragma: no cover
            row("Account signup", False, f"ENABLED but vault error: {exc}")
    else:
        row("Account signup", None, "disabled (default) — see docs/ACCOUNTS.md")

    # Needs-human notifications (ping you on CAPTCHA/login so you can solve locally)
    try:
        from honestapply import notify as _notify

        nprov = settings.honestapply_notify_provider
        if nprov and nprov != "none":
            ok = _notify.is_configured(settings)
            row("Notifications", ok, f"{nprov}" + ("" if ok else " · not fully configured"))
        else:
            row("Notifications", None, "off — set TELEGRAM_* or NTFY_TOPIC (docs/NOTIFICATIONS.md)")
    except Exception as exc:  # pragma: no cover
        row("Notifications", False, str(exc))

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
    note: str = typer.Option(None, "--note", "-n", help="Note to attach to this transition."),
    source: str = typer.Option("cli", "--source", help="Who is making the change (cli, email, ...)."),
) -> None:
    """Manually override a job's status.

    The change is attributed to ``--source`` (default ``cli``, not ``pipeline``)
    so the history and analytics can tell human/inbox overrides from automated
    pipeline steps. Prefer this over editing the DB directly, which bypasses the
    event log.
    """
    from honestapply.db.events import transition
    from honestapply.db.models import Job, JobEvent, Status
    from honestapply.db.session import session_scope

    if new_status not in Status.ALL:
        console.print(f"[red]Unknown status.[/red] Valid: {', '.join(Status.ALL)}")
        raise typer.Exit(1)
    with transition(source, note=note):
        with session_scope() as s:
            job = s.get(Job, job_id)
            if not job:
                console.print(f"[red]No job {job_id}.[/red]")
                raise typer.Exit(1)
            old = job.status
            if old == new_status:
                # No status change, so the before_flush listener records nothing.
                # A note here would be silently lost — instead append an explicit
                # same-status marker (mirroring the dashboard note endpoint) so it
                # still lands in the history; with no note, it's just a no-op.
                if note:
                    s.add(
                        JobEvent(
                            job_id=job.id,
                            from_status=old,
                            to_status=new_status,
                            source=source,
                            note=note,
                        )
                    )
                    console.print(f"[green]Job {job_id}: note added at {new_status}[/green]")
                else:
                    console.print(
                        f"[yellow]Job {job_id} already {new_status}; nothing changed.[/yellow]"
                    )
            else:
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


_WORKERS_HELP = "Jobs to process concurrently (default: HONESTAPPLY_PREPARE_WORKERS)."


@app.command()
def score(
    min_score: int = typer.Option(None, help="Override min fit score."),
    limit: int = typer.Option(None),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit to."),
    workers: int = typer.Option(None, help=_WORKERS_HELP),
) -> None:
    """Stage 3 — LLM fit scoring + gate. → run_score()"""
    from honestapply.stages.score import run_score

    n = run_score(min_score=min_score, limit=limit, ids=_parse_ids(ids), workers=workers)
    console.print(f"[green]Scored {n} jobs.[/green]")


@app.command()
def tailor(
    limit: int = typer.Option(None),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit to."),
    workers: int = typer.Option(None, help=_WORKERS_HELP),
) -> None:
    """Stage 4 — tailor resume per job (immutable-facts enforced). → run_tailor()"""
    from honestapply.stages.tailor import run_tailor

    n = run_tailor(limit=limit, ids=_parse_ids(ids), workers=workers)
    console.print(f"[green]Tailored {n} resumes.[/green]")


@app.command(name="cover-letter")
def cover_letter(
    limit: int = typer.Option(None),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit to."),
    workers: int = typer.Option(None, help=_WORKERS_HELP),
) -> None:
    """Stage 5 — generate per-job cover letters. → run_cover_letters()"""
    from honestapply.stages.cover_letter import run_cover_letters

    n = run_cover_letters(limit=limit, ids=_parse_ids(ids), workers=workers)
    console.print(f"[green]Generated {n} cover letters.[/green]")


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


@app.command(name="gmail-connect")
def gmail_connect() -> None:
    """Connect Gmail for inbox sync (one-time, honestapply-owned OAuth).

    The inbox reads Gmail through a local Gmail MCP server (see `.mcp.json`). This
    runs that server's one-time auth with least-privilege scopes: a browser opens
    for Google's consent screen, and the token is written to a LOCAL file
    (default ~/.gmail-mcp/credentials.json, chmod 600) that belongs to this
    install — not to any Claude account. Requires the OAuth client keys at
    gcp-oauth.keys.json first; see docs/GMAIL_SETUP.md for the Google Cloud setup.
    """
    s = get_settings()

    if not shutil.which("npx"):
        console.print(
            "`npx` (Node.js) is not on PATH. The local Gmail MCP server runs via "
            "npx — install Node.js 22+ first.",
            style="red", markup=False,
        )
        raise typer.Exit(1)

    keys = s.gmail_mcp_oauth_keys_file
    if not keys.exists():
        console.print(
            f"Missing OAuth client keys at {keys}.\n"
            "Create a Google OAuth client (Desktop app, or Web app with redirect "
            "http://localhost:3000/oauth2callback), download its JSON, and save it "
            f"there as gcp-oauth.keys.json. See docs/GMAIL_SETUP.md.",
            style="red", markup=False,
        )
        raise typer.Exit(1)

    if s.gmail_mcp_credentials_file.exists():
        console.print(f"[green]✓ Gmail already connected.[/green] Token at {s.gmail_mcp_credentials_file}")
        console.print("Run [cyan]honestapply inbox[/cyan] to sync recruiter emails into your tracker.")
        return

    # Hand off to the server's own auth command. It opens a browser, runs Google's
    # consent flow with ONLY the scopes we pass, and writes the token locally.
    scope_args = [f"--scopes={scope}" for scope in s.gmail_mcp_scopes.split()]
    console.print(
        f"Authorizing Gmail (scopes: {s.gmail_mcp_scopes}) — a browser will open. "
        f"Sign in and approve.\n"
    )
    proc = subprocess.run(
        ["npx", "-y", s.gmail_mcp_package, "auth", *scope_args], check=False
    )

    if proc.returncode == 0 and s.gmail_mcp_credentials_file.exists():
        console.print(f"\n[green]✓ Gmail connected.[/green] Token saved to {s.gmail_mcp_credentials_file}")
        console.print(f"Scopes: {s.gmail_mcp_scopes} (read-only — no send/modify tools are exposed).")
        console.print("Next: [cyan]honestapply inbox[/cyan] to sync recruiter emails into your tracker.")
    else:
        console.print(
            "\nAuth didn't complete (no token file written). Re-run "
            "[cyan]honestapply gmail-connect[/cyan], or check [cyan]honestapply doctor[/cyan].",
            style="yellow",
        )
        raise typer.Exit(1)


@app.command()
def inbox(
    days: int = typer.Option(None, "--days", help="Look back this many days (default: HONESTAPPLY_INBOX_LOOKBACK_DAYS)."),
    max_results: int = typer.Option(100, "--max", help="Max messages to scan this run."),
    min_confidence: int = typer.Option(None, help="Only move a job at/above this 0-100 confidence."),
) -> None:
    """Sync recruiter/ATS emails from Gmail into the tracker. → email.sync.run_inbox()

    A `claude` agent reads Gmail over MCP and classifies each message; honestapply
    then matches it to the jobs you've applied to and updates status (attributed
    to source='email' in job_events). Conservative: a job only moves on a
    confident, clear match that names the company, and never backwards.
    """
    from honestapply.email.agent import InboxAgentError
    from honestapply.email.sync import run_inbox

    try:
        r = run_inbox(lookback_days=days, max_results=max_results, min_confidence=min_confidence)
    except InboxAgentError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(1)
    console.print(
        f"[green]Inbox: scanned {r['scanned']} new email(s), matched {r['matched']}, "
        f"updated {r['updated']} job status(es).[/green]"
    )


# --- Site-account credentials (apply-stage auto-signup) --------------------------
accounts_app = typer.Typer(
    help="Manage saved portal-account credentials. Passwords live in the OS keyring "
    "or an encrypted vault and are NEVER printed — only site names/metadata are shown."
)
app.add_typer(accounts_app, name="accounts")


@accounts_app.command("list")
def accounts_list() -> None:
    """List portal accounts honestapply manages (names/emails only — no passwords)."""
    from honestapply.vault import get_vault

    rows = get_vault().list_sites()
    if not rows:
        console.print("No saved portal accounts yet.")
        return
    table = Table(title="Saved portal accounts (passwords not shown)")
    table.add_column("site")
    table.add_column("email")
    table.add_column("created")
    table.add_column("last used")
    for r in rows:
        table.add_row(
            r.get("site", ""),
            r.get("email", ""),
            (r.get("created_at", "") or "")[:10],
            (r.get("last_used_at", "") or "")[:10],
        )
    console.print(table)


@accounts_app.command("rotate")
def accounts_rotate(site: str = typer.Argument(..., help="Site host, e.g. careers.acme.com")) -> None:
    """Replace the stored password for a site with a fresh strong one."""
    from honestapply.vault import get_vault

    if get_vault().rotate(site):
        console.print(f"[green]✓ Rotated the stored password for {site}.[/green]")
    else:
        console.print(f"No saved account for {site}.", style="yellow")


@accounts_app.command("delete")
def accounts_delete(site: str = typer.Argument(..., help="Site host, e.g. careers.acme.com")) -> None:
    """Forget a site's stored credentials (removes the password and index entry)."""
    from honestapply.vault import get_vault

    if get_vault().delete(site):
        console.print(f"[green]✓ Deleted the stored account for {site}.[/green]")
    else:
        console.print(f"No saved account for {site}.", style="yellow")


@app.command()
def run(
    stages: list[str] = typer.Argument(None, help="Stages to run; default = all up to ready_to_apply."),
    prefilter: bool = typer.Option(False, "--prefilter", help="Run the cheap relevance gate before enrich."),
    ids: str = typer.Option(None, help="Comma-separated job IDs to limit every stage to."),
    limit: int = typer.Option(None, help="Cap how many jobs each LLM stage processes."),
    workers: int = typer.Option(None, help=_WORKERS_HELP),
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
        "score": lambda: __import__("honestapply.stages.score", fromlist=["run_score"]).run_score(limit=limit, ids=id_list, workers=workers),
        "tailor": lambda: __import__("honestapply.stages.tailor", fromlist=["run_tailor"]).run_tailor(limit=limit, ids=id_list, workers=workers),
        "cover-letter": lambda: __import__("honestapply.stages.cover_letter", fromlist=["run_cover_letters"]).run_cover_letters(limit=limit, ids=id_list, workers=workers),
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

    if not run_simulation(num_jobs=jobs):
        raise typer.Exit(1)


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
    token = getattr(module, "DASH_TOKEN", "")
    url = f"http://localhost:{port}/" + (f"?t={token}" if token else "")
    console.print(f"honestapply dashboard → [cyan]{url}[/cyan]")
    console.print("[dim]Open that URL (it carries a one-time key). Keep it to yourself.[/dim]")
    uvicorn.run(module.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
