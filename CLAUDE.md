# honestapply

Autonomous job-application pipeline: `discover → enrich → score → tailor → cover-letter → apply`.
All state lives in a local SQLite DB (`data/honestapply.db`). See `README.md` for the full
pipeline reference and `DECISIONS.md` for why things are built the way they are.

## Setup & everyday commands

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # 3.12 required (WeasyPrint/macOS quirks — see DECISIONS.md)
pip install -e ".[all]"
honestapply init        # creates .env + live configs from config/*.example.*, and the DB
honestapply doctor      # environment health check
honestapply simulate    # full offline pipeline run: stub LLM, fake jobs, no keys/network/browser
honestapply run         # discover → … → cover-letter (never applies)
honestapply apply --dry-run
pytest               # tests are offline; no API keys needed
```

Always validate changes with `honestapply simulate` and `pytest` first — both run with the
stub LLM provider and touch no network. `LLM_PROVIDER=claude_cli` in `.env` runs the real
pipeline with no API key (shells out to the user's Claude Code login).

## Hard rules

- **No fabrication, ever.** Every string in `resume_facts` (in `data/resumes/*.yaml`) is
  immutable: the tailor validator substring-checks each one in the output. Cover letters
  and form answers must only state things that are true of the user's profile. When a
  required field can't be answered truthfully from the profile, route the job to
  `needs_human` — never guess, never use placeholder text on a real form.
- **Personal data never goes to git.** `config/profile.json`, everything in
  `data/resumes/`, the DB, outputs, and archives are gitignored by design.
  Multiple people use this repo: never `git add -f` ignored files, and keep
  identifying details (names, paths, contact info) out of code, scripts, and docs.
  Templates for users live as `config/*.example.*` (e.g. `config/resume.example.yaml`).
- **Apply-stage safety is enforced in code** (first 3 jobs dry-run, ≥90s same-domain rate
  limit, daily cap 25/hard ceiling 50, triple dedup, LinkedIn Easy Apply disabled). Don't
  weaken these defaults; add new submission paths behind the same guards.

## Layout

- `src/honestapply/stages/` — one module per pipeline stage; `track.py` is status plumbing
- `src/honestapply/llm/` — provider abstraction (`claude_cli`, `anthropic`, `gemini`,
  `openai`, `stub`); prompts live in `src/honestapply/prompts/*.md`
- `src/honestapply/resume/` — YAML schema (`schema.py`), Jinja→WeasyPrint PDF renderer.
  Keep dates inline (not floated) in templates: floats scramble ATS text-extraction order
- `src/honestapply/ats/` — per-ATS detection + form metadata (Greenhouse/Lever/Ashby/…)
- `scripts/` — reporting: `build_tracker.py` (markdown board from the DB),
  `build_dashboard.py` (HTML dashboard; `HONESTAPPLY_OWNER_NAME` / `HONESTAPPLY_COWORK_XLSX`
  env vars), `fetch_job_descriptions.py` (archive JDs before postings vanish)
- `dashboard/api.py` — FastAPI review dashboard (`honestapply dashboard`); one table,
  every local artifact linked. Optional Next.js frontend in `dashboard/web/`
  (`npm install && npm run build` → static export served by FastAPI; works without it)

## Apply stage notes

The apply stage drives a browser via Playwright MCP (`.mcp.json` configures it; the
README covers install/permissions). CAPTCHAs are never solved — those jobs go to
`needs_human`. Browser logins persist in `~/.honestapply/browser-profile`.
