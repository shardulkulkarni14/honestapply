# honestapply

**A job-application agent that can't lie on your behalf — and that you can leave running.**

It finds roles, scores them, tailors a résumé, writes a cover letter, and fills in the
application. Everything runs on your own machine: your résumé never reaches a service,
and submissions come from your own browser and your own connection.

```bash
honestapply simulate   # full pipeline, offline, no API key, no browser — see it work first
```

## Why this one is different

**1. It can't fabricate. That's enforced, not requested.**
Every string under `resume_facts` in your résumé YAML is immutable. After the model
rewrites your CV for a job, each fact is substring-checked against the output; a mismatch
retries once and then routes the job to a human. No model sits in that verification path,
so "add a PhD" fails closed even if the model complies. Language claims get the same
treatment: if your profile says German is A2, no letter will call you fluent.

**2. The safety limits are constants in the code, not settings to upsell.**

| Guard | Value |
|---|---|
| First submissions of every run | dry run |
| Same-domain interval | ≥90s ± 30s jitter |
| Daily cap | 25, under a hard ceiling of 50 that config and env cannot raise |
| Dedup | three independent checks |
| CAPTCHAs | never solved — routed to `needs_human` |
| LinkedIn Easy Apply | off, behind two explicit opt-ins |

**3. Nothing leaves your machine.** State is one SQLite file. There's no account, no
server, and no subscription to cancel. It submits from your residential IP and real
contact details, because it isn't trying to look like anything other than you.

Most open-source job agents stop before the submit button. This one presses it — and the
reasons that's safe are in the code, not in this README. There is no proxy rotation, no
stealth driver, and no CAPTCHA solving here, by design. See [SECURITY.md](SECURITY.md)
for the threat model, including what it does *not* defend against.

## Quickstart

```bash
# 1. Environment (Python 3.12 venv recommended on macOS — see DECISIONS.md)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"

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
honestapply dashboard            # one-table review UI (FastAPI; optional Next.js frontend)
```

You never have to grant the apply stage anything: `--dry-run` plus
`honestapply apply-packet` gives you a filled, ready-to-submit packet you finish by hand.

## Pipeline

| Stage | Command | What it does |
|-------|---------|--------------|
| 1 | `honestapply discover` | JobSpy boards (Indeed/LinkedIn-search/Glassdoor/ZipRecruiter; Google is unreliable) + company boards on Greenhouse/Lever/Ashby **and SmartRecruiters/Workday** (reaches big employers that aren't on the startup ATSes). Triple-deduped. |
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

- **Job-board automation is gray-area.** Many boards' ToS restrict automated submission;
  LinkedIn especially carries ban risk (hence it's disabled by default). You are
  responsible for how you use this. The defaults err toward caution.
- **JobSpy scrapers are flaky** and often blocked from servers/datacenter IPs. Company
  board APIs (Greenhouse/Lever/Ashby) are reliable and are the recommended primary source.
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
- Licensed [AGPL-3.0](LICENSE).
