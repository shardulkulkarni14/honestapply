# Contributing to honestapply

Thanks for your interest. Contributions — bug reports, fixes, new ATS integrations,
docs — are welcome.

## Before you start

- **Sign the [CLA](CLA.md).** honestapply is dual-licensed (AGPL-3.0-or-later **and** a
  commercial edition), so contributors agree to a lightweight Contributor License
  Agreement: you keep ownership of your work, and the project keeps the right to include
  it in the commercial edition. Add a comment on your PR stating *"I have read the CLA and
  I agree."*
- **Never commit personal data or secrets.** The repo is designed around this — `.gitignore`
  denies `data/`, `config/`, `.env`, résumés, DBs, PDFs, and form snapshots by default. Do
  not `git add -f` anything ignored, and keep real names, emails, résumés, and application
  data out of code, tests, and docs. Tests and examples use placeholders (`Alex Example`).

## Dev setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # 3.12 recommended, >=3.11 supported
pip install -e ".[all]"
honestapply init        # creates .env + live configs from config/*.example.*, and the DB
honestapply init && honestapply simulate   # full offline pipeline: stub LLM, fake jobs, no keys/network/browser
pytest                  # offline; no API keys needed
```

## Making changes

- Validate with **`honestapply simulate`** and **`pytest`** before opening a PR — both run
  fully offline with the stub LLM provider.
- Keep the lint clean: `ruff check src/ tests/` and `black src/ tests/`.
- **Don't weaken the apply-stage safety guards** (first-N dry-run, same-domain rate limit,
  daily/lifetime caps, dedup, LinkedIn opt-ins). They're pinned by
  `tests/test_safety_invariants.py`; new submission paths must sit behind the same guards.
- Update `README.md` / `DECISIONS.md` if you change behaviour that they describe.

## Reporting bugs / requesting features

Open an issue using the templates. For anything security-sensitive, use **private
vulnerability reporting** (see [SECURITY.md](SECURITY.md)) rather than a public issue.
