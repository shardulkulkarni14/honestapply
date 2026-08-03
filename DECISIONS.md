# Decisions Log

Reasonable default choices made during the build, so they're auditable later.
Newest at the bottom of each phase.

## Phase 1 — Scaffold

- **Python 3.12 (not system 3.14).** System default is 3.14.4; `python-jobspy` and
  WeasyPrint can lag on the newest CPython wheels. Pinned a dedicated `.venv` on
  Python 3.12 (universal arm64 binary at `/usr/local/bin/python3.12`) for dependency
  stability. The host's brew `cairo`/`pango` (WeasyPrint deps) are arm64 — compatible.
- **Project root = this directory.** The original spec shows a top-level `honestapply/`
  folder; since the instruction was "build it in this directory", the current directory
  *is* that root (so `src/honestapply/...`, no redundant nesting).
- **Dependency extras instead of one flat list.** Core deps (Typer, Pydantic,
  SQLAlchemy, structlog, requests) install always. Heavier/stage-specific packages live
  in extras: `[resume]`, `[discover]`, `[llm]`, `[dashboard]`, `[dev]`, and `[all]`.
  Keeps each build phase installable and fast, and lets `doctor` report which groups
  are present. README quickstart uses `pip install -e ".[all]"`.
- **Build backend: hatchling** with `src/` layout (`packages = ["src/honestapply"]`).
- **Config precedence: env > config file > defaults.** Safety knobs (rate limit, daily
  cap, min score) and the LLM provider are read from `.env` first, then `config/*`,
  then code defaults. Implemented with pydantic-settings.
- **Default LLM model: `claude-sonnet-4-6`** for scoring/tailoring/cover letters
  (good quality/cost balance; Opus is overkill for these structured tasks).

## Build execution — parallel agents

- Built the **shared contracts** (config, DB models, LLM interface, resume schema +
  renderer, ATS detect, CLI, logging) first, then fanned out **5 parallel sub-agents**
  on file-disjoint scopes (resume templates+data, discover+enrich, LLM+score/tailor/cover,
  apply+configs, dashboard). Each used an isolated temp DB and `LLM_PROVIDER=stub` so they
  couldn't collide. Integration + `sim.py` + tests + docs were done centrally afterward.

## Phase 2 — Resume (ATS-friendly)

- **Single-column layout, real selectable text, plain-text headings, no layout tables.**
  Accent color used only for heading text + thin rules. Dates floated right (WeasyPrint
  supports basic `float`; flexbox is only partially supported). Helvetica/Arial, A4.
- Two resumes generated from the real CV: `default.yaml` (AI/GenAI Engineer) and
  `forward_deployed.yaml` (Forward Deployed / Solutions). Identical `resume_facts`
  (59 immutable strings), differing only in `name`, `target_keywords`, `summary_variants`.
- **macOS WeasyPrint:** native libs loaded via arch-matched `DYLD_FALLBACK_LIBRARY_PATH`
  set in `honestapply/__init__.py` (`/opt/homebrew/lib` on arm64). `/usr/local/lib` holds
  x86_64 glib that hard-fails dlopen if searched first — so we never add it on arm64.

## Phase 3 — Discover / Enrich

- **Company board APIs are the reliable source; JobSpy is best-effort.** Verified live
  Greenhouse tokens: anthropic, stripe, figma, vercel. Ashby: notion, linear, cursor,
  ramp (ramp exposes salary). Lever: palantir (none of the 8 starter co's use Lever).
  Unverified tokens are commented out in `employers.example.yaml` with how-to notes.
- Greenhouse `?content=true` returns the full (HTML-entity-encoded) JD, so enrich skips a
  network fetch for those. Lever `createdAt` is epoch-ms. Ashby uses `publishedAt`.

## Phase 4 — LLM layer (SDK specifics, anthropic 0.104 / google-genai / openai 2.x)

- Anthropic: `client.messages.create(model, max_tokens, messages, system=<str>, temperature)`,
  text at `message.content[0].text`. `system` is a top-level param, not a message.
- Gemini: the **new unified `google.genai`** SDK (`google-generativeai` was deprecated
  2025-11-30): `client.models.generate_content(model, contents, config=GenerateContentConfig(...))`.
- OpenAI: `client.chat.completions.create(...)`, text at `choices[0].message.content`.
- Tailor validator normalizes whitespace, then substring-checks every immutable fact;
  retries once on failure, else `needs_human`. Confirmed it catches a changed
  metric (test_tailor.py).

## Phase 5/6 — Apply

- Apply reads `HONESTAPPLY_APPLY_MOCK` at **call time** (mock path returns a synthetic RESULT
  + placeholder screenshots) so `simulate`/tests need no browser. Real mode shells out to
  `claude --dangerously-skip-permissions -p <instructions>` and parses the last
  `<<<RESULT>>>{...}<<<END>>>` block. `profile.example.json` is an anonymized template;
  everything the user must personalize is listed in a top-level `_TODO_confirm` array.

## Phase 7/8 — Dashboard, tests

- Originally Streamlit; replaced (2026-06) by a FastAPI backend + optional Next.js static
  export — one table linking every locally-stored artifact (JD, PDFs, answers, screenshots).
- (historical) Streamlit single-file; PDFs embedded as base64 iframes; stage buttons lazy-import so a
  missing/keyless stage never breaks page load. 13 pytest smoke tests, all green offline.
