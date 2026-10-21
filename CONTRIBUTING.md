# Contributing to honestapply

Thanks for your interest. Bug reports, fixes, new job sources, ATS adapters and docs are
all welcome. honestapply is MIT-licensed; there is no contributor agreement to sign.

## Good first contributions

- **A new discovery source.** Company-board and public-board adapters live under
  `src/honestapply/ats/` (detection + form metadata) and `src/honestapply/stages/discover.py`
  (fetching). A source must be free for the candidate, respect `robots.txt`, and come with
  an offline fixture test. Open an issue titled `Source: <board>` first so nobody doubles up.
- **A fabrication guard that is still missing.** The résumé tailor checks `resume_facts`
  verbatim; cover letters only have the language-level cap. Any check that makes an
  untrue claim fail closed, with a test, is welcome.
- **Dashboard and `needs_human` ergonomics.** Anything that makes the human queue faster to
  clear without automating the decision.

Issues labelled `good first issue` carry a rough time estimate. Comment `/assign` (or just
say you are on it) and it is yours; after 30 days of silence it goes back to the pool.

## Rules that never bend

- **Never commit personal data or secrets.** `.gitignore` denies `data/`, `config/`, `.env`,
  résumés, DBs, PDFs and form snapshots by default. Do not `git add -f` anything ignored,
  and keep real names, emails, résumés and application data out of code, tests and docs.
  Tests and examples use placeholders (`Alex Example`).
- **Don't weaken the apply-stage safety guards** (first-N dry-run, same-domain rate limit,
  daily cap and hard ceiling, dedup, LinkedIn opt-ins). They are pinned by
  `tests/test_safety_invariants.py`; new submission paths must sit behind the same guards.
- **Nothing may let the model fabricate.** A change that moves a verification step from
  code into a prompt will not be merged.

## Dev setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # 3.12 recommended, >=3.11 supported
pip install -e ".[all]"
honestapply init && honestapply simulate   # full offline pipeline: stub LLM, fake jobs, no keys/network/browser
pytest                                     # offline; no API keys needed
```

## Making changes

- Validate with `honestapply simulate` and `pytest` before opening a PR; both run offline.
- Keep the lint clean: `ruff check .`.
- Update `README.md` / `DECISIONS.md` if you change behaviour they describe, and add a line
  under `[Unreleased]` in `CHANGELOG.md`.
- Working with an AI coding agent is fine; a PR is judged on what it does and whether its
  tests prove it. State in the description what you checked yourself.

## Reporting bugs / requesting features

Open an issue using the templates. For anything security-sensitive, use **private
vulnerability reporting** (see [SECURITY.md](SECURITY.md)) rather than a public issue.
