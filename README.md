# honestapply

Autonomous, CLI-driven job-application pipeline.

`discover → enrich → score → tailor → cover-letter → apply` — all state in a local
SQLite DB, with a FastAPI + Next.js dashboard. Built for one person applying to many roles,
with hard safety rails (dry-run defaults, rate limits, daily caps, a `needs_human`
queue) and a strict **no-fabrication** rule on resume facts.

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

## Quickstart

```bash
# 1. Environment (Python 3.12 venv recommended on macOS — see DECISIONS.md)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"

# 2. Initialize: creates .env + live config files from examples, and the DB
honestapply init

# 3. Verify the environment
honestapply doctor

# 4. Prove the whole pipeline works offline (no API key, no browser):
honestapply simulate
```

### To run for real

1. **Pick an LLM provider** in `.env`. `LLM_PROVIDER=claude_cli` needs **no API key** —
   it reuses your local Claude Code login. Otherwise set `ANTHROPIC_API_KEY=...` with
   `LLM_PROVIDER=anthropic` (default; `gemini` is the near-free fallback). Without any
   of these you can still run `discover`/`enrich` and `simulate`.
2. **Fill in your profile**: edit `config/profile.json`. Search for the `_TODO_confirm`
   array — those fields (visa type, salary range, postal code, notice period, relocation,
   references, start date) are placeholders you must set.
3. **Create your resume(s)**: copy `config/resume.example.yaml` to
   `data/resumes/default.yaml` and replace every value with your real facts (the file
   documents the schema inline). Add one YAML per role family you target — the pipeline
   picks the best one per job via `target_keywords`. Everything under `resume_facts` is
   treated as immutable and verified after tailoring — **never put anything there that
   isn't true**.
4. **(Apply stage only) install Playwright MCP** and allow it:
   ```bash
   claude mcp add playwright -- npx @playwright/mcp@latest
   ```
   Then add `"mcp__playwright__*"` to `permissions.allow` in `.claude/settings.local.json`.
5. **Recommendation letters** (optional): drop PDFs in `data/recommendations/`; the apply
   stage offers them when an "additional documents" slot exists.

Then:

```bash
honestapply run                  # discover → enrich → score → tailor → cover-letter (NOT apply)
honestapply status               # counts per status, recent applications, success rate
honestapply apply --dry-run      # fill forms but stop before submit (first 3 always dry-run)
honestapply dashboard            # one-table review UI (FastAPI; optional Next.js frontend)
```

## Pipeline

| Stage | Command | What it does |
|-------|---------|--------------|
| 1 | `honestapply discover` | JobSpy boards (Indeed/LinkedIn-search/Glassdoor/ZipRecruiter; Google is unreliable) + company boards on Greenhouse/Lever/Ashby **and SmartRecruiters/Workday** (reaches big German employers like Bosch/Continental that aren't on the startup ATSes). Triple-deduped. |
| 1.5 | `honestapply prefilter` | Cheap **no-LLM** relevance gate: routes obvious non-fits (dead Indeed/LinkedIn hosts, interns/Werkstudent/warehouse/customer-service titles) to `skipped_low_fit` so the expensive LLM stages only see plausible roles. |
| 2 | `honestapply enrich` | Full JD via cascade: JSON-LD `JobPosting` → ATS CSS selectors → LLM fallback. |
| 3 | `honestapply score` | LLM fit score 1–10 + reasoning + matched/gap keywords. Gate at `min_score` (default 7). |
| 4 | `honestapply tailor` | Picks the best base resume by keyword match; LLM reorders/rephrases bullets and injects JD keywords **only where the fact already exists**. Output validated against immutable facts, then rendered to PDF. |
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

## Dashboard

`honestapply dashboard` (default `http://localhost:8501`, `--port` to change) serves **one
table with everything**: each application is a row linking every locally-stored artifact —

- **posting ↗** — the original job URL
- **JD** — the archived job description (survives the posting being taken down)
- **resume / cover** — the exact tailored PDFs that were submitted, opened inline
- **answers** — the form answers actually typed into that application
  (from `data/application_archive/answers_*.md`, fuzzy-matched by company)
- **shot** — pre/post-submit confirmation screenshots

Clickable status cards (applied / rejected / screening / …) filter the table; a search box
covers company, role, and location. Statuses from `data/active_pipeline.json` (interviews,
screenings) override the DB status per company, with the next action shown in the row.

Architecture: a FastAPI backend (`dashboard/api.py` — JSON API + local file serving) and
an optional Next.js frontend (`dashboard/web/`). Build the frontend once with
`cd dashboard/web && npm install && npm run build`; FastAPI serves the static export at
`/` — one command, one server. Without Node, a built-in fallback table keeps the
dashboard fully functional.

## Tracking & reporting

Beyond `honestapply status` / `honestapply dashboard`, three stdlib-friendly scripts keep a
durable record of the search:

| Script | Output | What it does |
|--------|--------|--------------|
| `python scripts/build_tracker.py` | `data/application_tracker.md` | Markdown board of every prepared/submitted/rejected application (fit score, status, apply link, login-required flag). Source of truth is the DB; re-run after every submission. |
| `python scripts/build_dashboard.py` | `data/dashboard.html` | One consolidated HTML dashboard merging the honestapply DB with any external Excel tracker — applications, rejections/skips, screening answers, interview prep in one place. |
| `python scripts/fetch_job_descriptions.py` | `data/application_archive/jd/` | Archives the verbatim JD for every application (Ashby/Greenhouse/Lever JSON APIs, Personio pages) so it survives the posting being taken down. |

`data/application_archive/` also keeps dated logs of the exact screening answers
submitted per application, for interview prep and consistency across re-applications.

The resume renderer is ATS-hardened from real screenings: inline (non-floated) dates so
PDF text-extraction order stays correct, clean clickable contact links that never break
mid-URL, and consecutive roles at the same company grouped under one header.

## Safety (enforced in code, not just docs)

- **First 3 jobs of any apply run are dry-run** unless `--no-safety` is passed.
- **Rate limit:** ≥90s ±30s jitter between submissions to the same domain (configurable).
- **Daily cap:** 25 real submissions/day default; hard ceiling 50 (`HARD_DAILY_CEILING`
  in `src/honestapply/config.py`, not overridable by config or env).
- **Triple dedup:** DB unique constraint + per-`job_id` Application check + URL-hash check.
- **`needs_human` queue:** CAPTCHA, auth challenges, and unanswerable required fields land
  here instead of guessing. The dashboard surfaces them.
- **No fabrication:** every string in a resume's `resume_facts` must survive tailoring
  verbatim (substring-checked); on failure the job is retried once, then `needs_human`.
- **LinkedIn Easy Apply is OFF** unless `--enable-linkedin-easy-apply` *and*
  `HONESTAPPLY_LINKEDIN_CONFIRM=I-UNDERSTAND-LINKEDIN-BAN-RISK`.

## Limitations (honest)

- **Job-board automation is gray-area.** Many boards' ToS restrict automated submission;
  LinkedIn especially carries ban risk (hence it's disabled by default). You are
  responsible for how you use this. The defaults err toward caution.
- **JobSpy scrapers are flaky** and often blocked from servers/datacenter IPs. Company
  board APIs (Greenhouse/Lever/Ashby) are reliable and are the recommended primary source.
- **The apply stage needs Playwright MCP + a logged-in browser** for portals that require
  auth. Log in once in the persisted profile (`~/.honestapply/browser-profile`); sessions
  carry over. No CAPTCHA solving — those go to `needs_human`.
- **Scoring/tailoring quality tracks your resume baseline and the model.** The score gate
  is doing real work — trust it. Lower `min_score` to see more jobs; raise it if your
  response rate is low.
- **It won't make you qualified for roles you aren't.** It tailors and submits faster; it
  doesn't invent experience.

See `DECISIONS.md` for design choices and the per-stage notes.
