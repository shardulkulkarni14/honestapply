<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
  <img alt="honestapply" src="docs/logo-light.svg" height="44">
</picture>

**The job-application agent that presses Submit, and can prove why that is safe.**

[![CI](https://github.com/shardulkulkarni14/honestapply/actions/workflows/ci.yml/badge.svg)](https://github.com/shardulkulkarni14/honestapply/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11–3.13](https://img.shields.io/badge/python-3.11%E2%80%933.13-blue.svg)](pyproject.toml)
[![Latest release](https://img.shields.io/github/v/release/shardulkulkarni14/honestapply?label=release)](https://github.com/shardulkulkarni14/honestapply/releases)
[![Hired with honestapply](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fshardulkulkarni14%2Fhonestapply%2Fmain%2Fdocs%2Fhired-count.json&query=%24.count&label=hired%20with%20honestapply&color=2ea44f)](HIRED.md)

**[See it work in a minute](#see-it-work-in-a-minute) · [What it will not do](#what-it-will-not-do) · [Quickstart](#quickstart) · [Works inside Claude Code](#works-inside-claude-code-and-codex) · [Safety](SECURITY.md)**

</div>

Most open-source job tools stop at the form and hand it back to you. honestapply goes
through with it: it finds roles, scores them, tailors a résumé, writes the cover letter,
fills the form and submits, while you sleep if you want. That only works if the guardrails
live **in the code, where the model cannot talk its way past them**, and that is what this
project is. Every fact in your résumé is checked verbatim after tailoring; a language you
rate A2 is never called fluent; a daily ceiling no config can raise; the first submissions
of every run dry so you see the forms before any go out. I built it during my own search
in Germany, after watching "apply for you" tools send skills I did not have to jobs I did
not want.

Everything runs on your own machine. Your résumé never reaches a service, there is no
account, and submissions come from your own browser and your own connection.

## See it work in a minute

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
honestapply init && honestapply simulate   # whole pipeline, offline, no API key, no browser
```

`simulate` runs every stage end to end on fake jobs with a stub model and prints what each
stage produced. It is the honest demo: nothing is mocked around, the real code runs.

## What it will not do

Each line below is enforced by code, not asked of the model in a prompt, and all but the last are pinned by tests.

- **Submit before you have seen it work.** `honestapply apply` is dry-run by default and
  fills forms without sending; `--no-dry-run` turns submission on for a run, and the first
  jobs of that run are still dry so you can inspect them. `honestapply run` never reaches
  the apply stage at all (`src/honestapply/stages/apply.py`).
- **Invent a fact.** Every string under `resume_facts` must survive tailoring verbatim.
  The check is a substring comparison with no model in the path; a miss retries once, then
  routes the job to `needs_human` (`src/honestapply/stages/tailor.py`).
- **Overstate a language.** CEFR levels in your profile cap what a cover letter may claim.
- **Apply below the fit gate.** Jobs under `min_score` (default 7 of 10) are
  `skipped_low_fit` with the model's reasoning kept, so you can read why.
- **Send more than 50 applications a day.** The default cap is 25; the ceiling of 50 is a
  constant that no config or environment variable can raise (`src/honestapply/config.py`).
- **Solve a CAPTCHA, rotate proxies, or use a stealth browser.** Those forms go to
  `needs_human`, or to `honestapply apply-packet`, which hands you a filled packet to finish.
- **Touch LinkedIn Easy Apply** without two explicit opt-ins.
- **Phone home.** No telemetry, no server, one SQLite file.

The threat model, including what this does *not* defend against, is in
[SECURITY.md](SECURITY.md).

## Works inside Claude Code and Codex

You do not need the dashboard to drive it. Open the repo in Claude Code, Codex, OpenCode
or Cursor and ask for what you want ("score the new Munich postings", "what is waiting
on me", "dry-run the three best"). [`AGENTS.md`](AGENTS.md) is the operator guide the
agent follows, including the rule that a real submission needs your yes in that session.
A Claude Code skill in `.claude/skills/honestapply/` points at the same guide.

## Quickstart

```bash
# 1. Environment (Python 3.12 venv recommended on macOS — see DECISIONS.md)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
# or, without a checkout:  pipx install "honestapply[all] @ git+https://github.com/shardulkulkarni14/honestapply"

# 2. Creates .env + live config from the examples, seeds an example résumé, makes the DB
honestapply init

# 3. Check your environment
honestapply doctor

# 4. Prove the whole pipeline works — offline, stub LLM, fake jobs, no browser
honestapply simulate
```

`simulate` is the honest demo: it exercises every stage end to end with no keys, no
network and no browser, and prints a table of what each stage produced.

> **Note:** PDF rendering currently uses WeasyPrint, which needs system Pango
> (`brew install weasyprint`, `apt install weasyprint`). A Chromium-based renderer that
> removes this prerequisite is in progress.

```
┌──────────┐   ┌────────┐   ┌───────┐   ┌────────┐   ┌──────────────┐   ┌───────┐
│ discover │──▶│ enrich │──▶│ score │──▶│ tailor │──▶│ cover-letter │──▶│ apply │
└────┬─────┘   └───┬────┘   └───┬───┘   └───┬────┘   └──────┬───────┘   └───┬───┘
     │             │            │           │               │               │
  JobSpy +     JSON-LD →     LLM fit     pick best       per-job LLM    Claude Code
  Greenhouse/  ATS CSS →     score 1-10  resume +        cover letter   + Playwright
  Lever/Ashby  LLM fallback  (gate ≥7)   LLM rewrite     → PDF          MCP fills the
  board APIs                             (facts locked)                 form (dry-run
                                         → PDF                          first)
     └──────────────────────── SQLite (jobs / applications / run_logs) ──────────┘
                          status: discovered → … → applied | needs_human | failed
```

## Running it for real

1. **Pick an LLM provider** in `.env`. Three ways to avoid an API key entirely:
   `LLM_PROVIDER=claude_cli` reuses your local Claude Code login; or point
   `OPENAI_BASE_URL` at any OpenAI-compatible server; or run neither and use
   `simulate`. Otherwise set `ANTHROPIC_API_KEY=...` with `LLM_PROVIDER=anthropic`
   (default; `gemini` is the near-free fallback).

   ```bash
   # Local, no key, nothing leaves the machine
   LLM_PROVIDER=openai
   OPENAI_BASE_URL=http://localhost:11434/v1   # Ollama; LM Studio, vLLM, llama.cpp alike
   OPENAI_MODEL=qwen3:14b
   ```

   The same field reaches OpenRouter, Groq, Together, DeepSeek and Fireworks.
   **Read this before going fully local:** the immutable-facts validator makes a
   weak model's fabrication fail loudly when tailoring a résumé, but cover letters
   have no equivalent hard check, and smaller models over-claim more and more
   quietly. Local inference is a good fit for the high-volume scoring pass; it is a
   bad fit for the browser apply loop, where a mistake is a real submission to a
   real employer.
2. **Fill in your profile**: edit `config/profile.json`. Search for the `_TODO_confirm`
   array — those fields (visa type, salary range, postal code, notice period, relocation,
   references, start date) are placeholders you must set.
3. **Write your résumé(s)**: `honestapply init` seeds `data/resumes/default.yaml` from the
   example. Replace every value with your real facts — the file documents the schema
   inline. Add one YAML per role family you target; the pipeline picks the best one per
   job via `target_keywords`. Everything under `resume_facts` is treated as immutable and
   verified after tailoring — **never put anything there that isn't true.**
4. **(Apply stage only) install Playwright MCP** and allow it:
   ```bash
   claude mcp add playwright -- npx @playwright/mcp@latest
   ```
   Then add `"mcp__playwright__*"` to `permissions.allow` in `.claude/settings.local.json`.
   Note that the apply stage runs the browser agent with permission prompts disabled —
   [SECURITY.md](SECURITY.md) explains exactly what that grants and what bounds it.
5. **Recommendation letters** (optional): drop PDFs in `data/recommendations/`; the apply
   stage offers them when an "additional documents" slot exists.

Then:

```bash
honestapply run                  # discover → enrich → score → tailor → cover-letter (NOT apply)
honestapply status               # counts per status, recent applications, success rate
honestapply apply --dry-run      # fill forms but stop before submit (first 3 always dry-run)
honestapply dashboard            # one-table review UI (FastAPI + a single self-contained page)
```

You never have to grant the apply stage anything: `--dry-run` plus
`honestapply apply-packet` gives you a filled, ready-to-submit packet you finish by hand.

## Pipeline

| Stage | Command | What it does |
|-------|---------|--------------|
| 1 | `honestapply discover` | JobSpy boards (Indeed/LinkedIn-search/Glassdoor/ZipRecruiter; Google is unreliable), the **Arbeitsagentur** Jobbörse (official read API; Germany's largest public job database), and company boards on Greenhouse/Lever/Ashby, **SmartRecruiters/Workday** (big employers) and **Personio/Recruitee/Workable/Teamtailor** (the feeds German SMEs and start-ups use). Arbeitsagentur is a *source*, not an apply channel: its listing page gates the employer link behind a CAPTCHA, so discovery swaps it for the employer's own posting on a public board when the same title is found there, and whatever still points at the listing page is dropped by prefilter and refused by apply. Triple-deduped. |
| 1.5 | `honestapply prefilter` | Cheap **no-LLM** relevance gate: routes obvious non-fits (dead Indeed/LinkedIn hosts, interns/Werkstudent/warehouse/customer-service titles) to `skipped_low_fit` so the expensive LLM stages only see plausible roles. |
| 2 | `honestapply enrich` | Full JD via cascade: JSON-LD `JobPosting` → ATS CSS selectors → LLM fallback. |
| 3 | `honestapply score` | LLM fit score 1–10 + reasoning + matched/gap keywords. Gate at `min_score` (default 7). |
| 4 | `honestapply tailor` | Picks the best base resume by keyword match. With one **master** résumé (`tailoring: select`) the LLM selects the bullets, projects and skill items that fit the job and drops the rest; every kept string must be a verbatim copy from the master, in the same entry, and nothing non-optional may disappear. With the default `preserve` mode it may only reorder, pick a summary and add JD keywords to skills. Output validated in code, then rendered to PDF. |
| 5 | `honestapply cover-letter` | Per-job cover letter → PDF. Guarded against language **over-claims** (CEFR levels from `profile.language_levels` cap the résumé; e.g. never "fluent/native German" when German is A2). |
| 6 | `honestapply apply` | Detects the ATS, builds a per-job instruction file, drives Claude Code + Playwright MCP to fill (and optionally submit) the form. Dry-run by default; skips dead Indeed/LinkedIn hosts; dedup only blocks on a *real, completed* submission (dry-runs no longer bury a job). |
| — | `honestapply apply-packet` | For captcha/login-walled forms (Lever hCaptcha, Workday): assembles a ready-to-submit packet (apply URL + mapped field answers + merged supporting docs) instead of burning a browser session. |
| — | `honestapply docs-bundle` | Merges `profile.supporting_documents` (transcripts, reference letters, Arbeitszeugnis) into one PDF for forms that require a single combined upload. |
| — | `honestapply status` | Pipeline counts, recent applications, success rate — in the terminal. |
| — | `honestapply dashboard` | One-table web UI: every application with its archived JD, submitted resume/cover PDFs, form answers, and screenshots one click away. See [Dashboard](#dashboard). |
| — | `honestapply simulate` | Full offline dry run with fake jobs + stub LLM — no keys/network/browser. |
| — | `honestapply mark <id> <status>` | Manual status override. |

Stages accept `--ids 101,102` and `--limit N` (and `honestapply run --prefilter --ids …`)
to drive a curated/bounded subset instead of draining the whole backlog.

The prepare stages (enrich/score/tailor/cover-letter) can work on several jobs at
once: `--workers N` on `score`, `tailor`, `cover-letter`, `run` and
`scripts/batch_drive.py`, or `HONESTAPPLY_PREPARE_WORKERS` in `.env` (the shell
loops pass `PREPARE_WORKERS`). It ships at **1** — identical to the serial
behaviour — and **3** is the recommended value once a run looks right: the
stages are LLM-bound, so a few threads overlap the waits without tripping
Claude Code's usage limits. Each job still commits one stage at a time through a
compare-and-set, so two workers (or two processes) never advance the same job
twice, and a rate-limit error pauses every worker instead of burning the queue.
`HONESTAPPLY_SCORE_PROVIDER` optionally scores with a different provider (e.g.
`anthropic` over the API) while the rest stays on the key-free CLI. **Apply is
never parallel**, whatever this is set to.

## Dashboard

`honestapply dashboard` (default `http://localhost:8501`, `--port` to change) serves **one
table with everything**: each application is a row linking every locally-stored artifact —

- **posting ↗** — the original job URL
- **JD** — the archived job description (survives the posting being taken down)
- **resume / cover** — the exact tailored PDFs that were submitted, opened inline
- **shot** — pre/post-submit confirmation screenshots

Clickable status cards (applied / rejected / screening / …) filter the table; a search box
covers company, role, and location.

**Phase history.** Every row has a ▸ toggle that expands the job's full timeline — each
status transition (`enriched → scored → tailored → …`) with its timestamp and source badge
(`pipeline` / `cli` / `dashboard`), and the apply attempts interleaved in place. Add a note
from the input at the bottom of the drawer: it appends a dashboard-sourced, same-status
marker to the log. The history is **append-only and read-only** — timestamps and past
transitions are never edited, so the log stays a faithful audit trail. Manual status
changes made from the CLI (`honestapply mark <id> <status> --note … --source …`) or the
dashboard's status dropdown are recorded the same way, attributed to whoever made them.

Architecture: a FastAPI backend (`dashboard/api.py` — JSON API + local file serving)
that serves one self-contained page (`dashboard/index.html`) at `/`. No build step, no
Node, no bundler — `honestapply dashboard` is one command and one server.

## Tracking & reporting

Beyond `honestapply status` / `honestapply dashboard`, three stdlib-friendly scripts keep a
durable record of the search:

| Script | Output | What it does |
|--------|--------|--------------|
| `python scripts/build_tracker.py` | `data/application_tracker.md` | Markdown board of every prepared/submitted/rejected application (fit score, status, apply link, login-required flag). Source of truth is the DB; re-run after every submission. |
| `python scripts/fetch_job_descriptions.py` | `data/application_archive/jd/` | Archives the verbatim JD for every application (Ashby/Greenhouse/Lever JSON APIs, Personio pages) so it survives the posting being taken down. |

The resume renderer is ATS-hardened from real screenings: inline (non-floated) dates so
PDF text-extraction order stays correct, clean clickable contact links that never break
mid-URL, and consecutive roles at the same company grouped under one header.

## Safety, in detail

- **First 3 jobs of any apply run are dry-run** unless `--no-safety` is passed.
- **Rate limit:** ≥90s ±30s jitter between submissions to the same domain (configurable).
- **Daily cap:** 25 real submissions/day default; hard ceiling 50 (`HARD_DAILY_CEILING`
  in `src/honestapply/config.py`, not overridable by config or env).
- **Triple dedup:** DB unique constraint + per-`job_id` Application check + URL-hash check.
- **`needs_human` queue:** CAPTCHA, auth challenges, and unanswerable required fields land
  here instead of guessing. The dashboard surfaces them.
- **No fabrication:** every string in a resume's `resume_facts` must survive tailoring
  verbatim (substring-checked); on failure the job is retried once, then `needs_human`.
- **Untrusted input:** job descriptions are fenced before reaching any prompt, and the
  browser agent is instructed that page content is data, never instructions.
- **LinkedIn Easy Apply is OFF** unless `--enable-linkedin-easy-apply` *and*
  `HONESTAPPLY_LINKEDIN_CONFIRM=I-UNDERSTAND-LINKEDIN-BAN-RISK`.

These are asserted by tests, so weakening one fails the build rather than passing quietly.

## Limitations (honest)

- **Job-board automation is gray-area — on both discovery and submission.** Scraping
  search results (the JobSpy sources, LinkedIn search included) and automated submission
  both run against many boards' ToS; LinkedIn especially carries ban risk (hence Easy Apply
  is disabled by default). You are responsible for how you use this. The defaults err toward
  caution, and the company board APIs (Greenhouse/Lever/Ashby/SmartRecruiters/Personio/
  Recruitee/Workable/Teamtailor) plus the Arbeitsagentur read API are the recommended,
  ToS-clean primary sources.
- **JobSpy scrapers are flaky** and often blocked from servers/datacenter IPs. Company
  board APIs are reliable and are the recommended primary source.
- **Arbeitsagentur listings without an employer link cannot be applied to automatically.**
  The employer-board lookup finds the posting for companies on a public ATS; employers
  with their own portal (REWE, ALTEN, …) stay source-only and need a manual application
  (`honestapply apply-packet` prepares the documents).
- **The apply stage needs Playwright MCP + a logged-in browser** for portals that require
  auth. Log in once in the persisted profile (`~/.honestapply/browser-profile`); sessions
  carry over. No CAPTCHA solving — those go to `needs_human`.
- **Volume is not the goal, and shouldn't be.** Tailored applications convert several
  times better than generic ones, and high per-day volume from one origin is what
  ATS-side spam detection looks for. The caps are low on purpose.
- **Scoring/tailoring quality tracks your résumé baseline and the model.** The score gate
  is doing real work — trust it. Lower `min_score` to see more jobs; raise it if your
  response rate is low.
- **It won't make you qualified for roles you aren't.** It tailors and submits faster; it
  doesn't invent experience.

## More

- [SECURITY.md](SECURITY.md) — threat model, prompt injection, what this doesn't defend against
- [DECISIONS.md](DECISIONS.md) — why things are built the way they are

## License

MIT. Copyright © 2026 Shardul Kulkarni. See [LICENSE](LICENSE).

## Contributing

Contributions are welcome and there is nothing to sign. A new discovery source, a
fabrication check that is still missing, or anything that makes the `needs_human` queue
faster to clear are the best first pull requests; see [CONTRIBUTING.md](CONTRIBUTING.md).

If honestapply was part of a search that worked out, add your card to
[HIRED.md](HIRED.md) with the *I got hired* issue template. The count badge above reads
from that file and nothing else.
