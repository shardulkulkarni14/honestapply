# Changelog

All notable changes to honestapply are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor
versions may still change behaviour).

## [Unreleased]

### Added
- **Master résumé with per-job selection (`tailoring: select`).** A base résumé
  can now be a superset: the tailor asks the LLM to keep the bullets, projects
  and skill items that fit the posting and drop the rest, instead of shipping
  the whole file. A new validator accepts the output only if every kept string
  is a verbatim copy from the master in the same entry, contact/education/
  languages are unchanged, skills items and awards are subsets, and every
  entry not marked `optional: true` survives with at least one bullet; one
  retry, then `needs_human`. The kept facts are written next to the PDF
  (`resume.tailored.yaml`) and provenance attests the PDF against them.
  Motivation: with many role-specific variants the thinnest file often won the
  keyword match (one 11-bullet variant was used for 13 of 36 recent
  submissions), so the detailed résumé was rarely the one sent. The default
  `preserve` mode is unchanged.

### Changed
- **Picker employer blacklist is now time- and count-based** (`scripts/pick_candidates.py`,
  `blocked_companies`). Previously any employer that had ever sent a rejection was
  skipped for good. An employer is now skipped only if it rejected the profile within
  `--reject-days` (default 60), has `--max-rejections` or more rejections in total
  (default 3), or has `--ghost-apps` or more applications (default 2) unanswered for
  longer than `--ghost-days` (default 21). Employer names are compared after
  stripping legal forms (`company_key`), so "Example GmbH" and "Example" match.
- **Scoring applies a hard language rule.** A posting that names a German level the
  profile does not have (B1–C2, "gute Deutschkenntnisse", "in Wort und Schrift",
  "fließend / verhandlungssicher", "fluent / professional" or German as mandatory) is
  capped at score 4 with the gap flag `german_requirement`. A posting merely written in
  German is not a requirement and is not penalised.

## [0.5.1] — 2026-10-25

### Fixed
- **Employer-board resolver no longer adopts a stranger's board.** A short slug guess
  such as a company's first word ("amazon", "genius", "westhouse") can land on an
  unrelated tenant; the first live board was cached as the company's and the real one
  never probed. All boards that answer are now kept as candidates, and one is adopted
  only once a title matches. Seen on the first run over 115 stored Arbeitsagentur
  listings: 3 of the 12 "found" boards belonged to other companies.
- **SmartRecruiters probe paginates.** Large tenants (ALTEN: 1,176 postings) list the
  wanted role beyond the first 100, so the title never matched.

## [0.5.0] — 2026-10-25

### Added
- **Arbeitsagentur is a source, not an apply channel.** Its listing page hides the
  employer's apply link behind an image CAPTCHA, which parked three of five such jobs
  on a live run. Discovery now looks the same title up on the employer's public ATS
  board (Greenhouse, Lever, Ashby, SmartRecruiters, Personio, Recruitee, Workable,
  Teamtailor; slug guesses from the company name, board cached per company in
  `data/employer_boards.json`) and stores the employer's posting URL instead. Jobs that
  still point at `arbeitsagentur.de` are dropped by `prefilter` and refused by `apply`
  before a browser session is spent. `HONESTAPPLY_ARBEITSAGENTUR_APPLY=true` opts back
  in; `HONESTAPPLY_ARBEITSAGENTUR_RESOLVE_EMPLOYER=false` turns the lookup off.
- **Personio, Recruitee, Workable and Teamtailor board adapters** (`ats: personio |
  recruitee | workable | teamtailor` in `employers.yaml`), the feeds most German SMEs
  and start-ups publish. Read-only public JSON/XML, no auth, no scraping. Personio and
  Teamtailor accept `host:` for custom career domains; Personio accepts `language: en`
  and merges the translation over the default feed (the translated feed is empty for
  untranslated jobs).
- `detect_ats` recognises the new hosts plus `join` and `arbeitsagentur`;
  `is_source_only()` names boards that can be sourced but never applied through.
- `config/employers.example.yaml` documents the new feeds and ships a German section
  of verified Personio/Recruitee/Teamtailor boards.

### Fixed
- **Cover-letter language guard accepts an honest disclosure of the required level.**
  "Your posting asks for fluent German; my German is currently A2" was flagged as a
  fluency claim, because the guard saw the word next to the language name without
  reading the rest of the sentence. A level that sits in the same sentence as the
  candidate's real, lower level (A2, basic, Grundkenntnisse, …) is now treated as the
  truthful disclosure the prompt asks for. "The role requires fluent German, which I
  bring" is still caught. Found on a live run: eight letters for roles that list a
  German requirement were rejected twice each and the jobs routed to `needs_human`.
  The prompt now also asks the writer to put the real level in the same sentence
  whenever it mentions the requirement.
- **Managed portal passwords are 16 characters, not 20.** SAP SuccessFactors career
  sites cap passwords at 18 characters, so the auto-signup's 20-character password was
  rejected on a live run and the job routed to `needs_human`. The default for new and
  rotated passwords is now 16 mixed-class characters, inside the common 8-18 window.
  Existing stored passwords are untouched; `honestapply accounts rotate <site>` re-issues one.

## [0.4.1] — 2026-10-21

### Fixed
- **Cover-letter language guard no longer misreads a neighbouring language's
  level.** "English (C1) and German (A2)" was flagged as a C1 claim for German,
  because the guard only checked that a level and a language name sat within a
  few characters of each other. A level is now attributed to the nearest
  language name, so that sentence passes while "fluent English and C1 German"
  is still caught. Found on a live run: a correct letter was rejected twice and
  the job routed to `needs_human`.

### Changed
- README tagline and opening now state the project's own stance: it is the agent that
  presses Submit, with the guards that make that safe enforced in code. The 0.4.0 wording
  ("won't press Submit until you say so") read like a no-auto-submit tool, which this is not.

## [0.4.0] — 2026-10-21

### Changed
- **License is now MIT** (was AGPL-3.0-or-later). The Contributor License Agreement and
  the commercial-licensing page are gone; there is nothing to sign and no paid edition.
- **README rewritten around what the tool refuses to do.** The "What it will not do"
  section lists each guard with the file that enforces it; submission is described as
  opt-in per run, which is how `apply` has always behaved. The stale "early (v0.2.0)"
  badge is replaced by a release badge.
- `CONTRIBUTING.md` names good first contributions (a discovery source with an offline
  fixture, a missing fabrication check) and the rules that never bend.

### Added
- `AGENTS.md`: the operator guide for an AI coding agent running the pipeline on a
  person's behalf (confirmation before `--no-dry-run`, never editing facts to satisfy a
  form, postings are data not instructions, personal data stays out of git). A Claude
  Code skill in `.claude/skills/honestapply/` points at it; `CLAUDE.md` links it.
- `HIRED.md` and the *I got hired* issue template, with `docs/hired-count.json` feeding a
  README badge. The count only changes when a verified card is added.

No pipeline behaviour changed in this release.

## [0.3.2] — 2026-10-20

### Fixed
- **Tailor no longer picks a relocation-specific base résumé for a job
  elsewhere.** Base résumés were ranked by keyword overlap alone, so a variant
  whose header reads "Relocating to Hyderabad, India" (with the India phone
  number) could win for a Munich posting — and did, on a live run. A variant
  that declares a relocation is now eligible only when the job's location names
  that city or country (ISO suffixes like `, IN` / `, DE` count); location-neutral
  résumés fit everywhere. If every base résumé is a relocation variant for
  somewhere else, the job routes to `needs_human` instead of guessing.

## [0.3.1] — 2026-10-19

### Fixed
- **Dashboard stale-cookie lockout.** The local dashboard now prefers the fresh
  `?t=` URL token over any `ha_token` cookie, so a stale cookie from an earlier
  run (cookies are shared across ports on localhost) can no longer shadow a valid
  token and 401 you out. Opening from the terminal URL always works.
- **Test suite no longer inherits the developer's `.env` / shell overrides.**
  Settings are now hermetic under pytest (the repo's `.env` is not read and
  `HONESTAPPLY_*` shell variables are dropped), so the suite always exercises the
  shipped defaults. Previously a personal opt-in such as
  `HONESTAPPLY_ENABLE_ACCOUNT_SIGNUP=true` in `.env` silently flipped the
  account-walled → `needs_human` safety-routing tests to failures. Runtime
  behaviour is unchanged: account-walled jobs still route to `needs_human`
  unless account signup is explicitly enabled.

## [0.3.0] — 2026-10-19

The German market, end to end: the Bundesagentur für Arbeit Jobbörse as a
first-class discovery source, and a deterministic ATS apply fast-path.

### Added
- **Deterministic ATS fill-plan fast-path.** For known ATS forms, apply
  precomputes the truthful, placeholder-free fields (name, email, phone, links,
  location) from the profile and hands the agent an explicit plan to execute
  rather than reason about field-by-field. Rule 0 still holds: anything the live
  page contradicts or that isn't planned falls back to the normal steps
  (including `needs_human`); nothing is invented.
- **Arbeitsagentur (Bundesagentur für Arbeit) discovery source.** A new
  `board: arbeitsagentur` queries Germany's largest public job database through
  its official read API (`pc/v6/jobs`) — no scraping, no anti-bot circumvention.
  Results flow through the normal pipeline; `enrich` fetches each full posting.
  Personal-use posture: listings stay in the local DB and are never
  redistributed (the data is the agency's, limited to job-placement use).
- **Needs-human notifications.** When a job is routed to `needs_human` during
  apply, send a phone notification (Telegram or ntfy) so you can step in. Opt-in
  via `HONESTAPPLY_NOTIFY_PROVIDER`; notify-only (no CAPTCHA auto-solving).
- **Optional Chromium PDF renderer** behind `RESUME_RENDERER=chromium`, so users
  can avoid the system Pango dependency. WeasyPrint stays the default; `playwright`
  is an opt-in extra.
- **Archive (soft-delete) jobs** from the dashboard (per-row button with undo +
  a "Show archived" toggle) or the CLI (`honestapply archive`/`unarchive <id>`).
  Backed by a nullable `jobs.archived_at`; history is never touched.
- **Professional README header** with an SVG wordmark and true status badges.
- **Opt-in work-auth geo filter.** With `HONESTAPPLY_SKIP_UNAUTHORIZED_COUNTRIES`
  on, roles located in a country the profile establishes you cannot work in (a
  confident "not authorised", needs sponsorship) are retired before tailor, so
  no résumé/cover-letter compute is spent on them. Conservative: unknown, remote,
  or unsettled countries are always kept. Off by default.

### Changed
- The dashboard funnel, CLI status counts, and analytics all exclude archived jobs.
- The per-employer lifetime cap is now applied **before tailor**, not only at
  apply: employers already at the cap have their queued roles retired up front,
  so the pipeline no longer spends résumé/cover-letter compute on jobs the apply
  stage would skip. The apply-time guard stays as the backstop, and both now
  share one submission counter (`track.company_total_submissions`).

### Removed
- The dead Next.js dashboard frontend (`dashboard/web/`); the dashboard is a
  single self-contained page.

## [0.2.0] — 2026-10-08

Inbox awareness, hands-off account creation with secrets you can't read, and a
product-grade review dashboard.

### Added
- **Gmail inbox sync.** A local, honestapply-owned Gmail MCP server (read-only
  scope) classifies replies and advances each job's status automatically —
  rejections, screenings, interview invites. No Gmail API keys in the app; the
  token lives under `~/.gmail-mcp`.
- **Account auto-signup with a secrets vault.** Site-account passwords are kept
  in an OS keyring (or a Fernet-encrypted file) and injected at the browser
  boundary by a secret-injection proxy, so the apply agent never sees them.
  Email verification (OTP / magic link) is completed automatically via the
  inbox. Off by default — see `docs/ACCOUNTS.md`.
- **Product dashboard.** A plain-language review UI served as a single
  self-contained page (no build step): run control from the browser, status
  filter chips, pagination, a per-job phase-history timeline, append-only
  notes, editable status, and a present mode for sharing.
- **Parallel prepare.** `discover → … → cover-letter` runs through a bounded
  worker pool with compare-and-set per stage.
- Configurable apply model and a goal-watchdog; `docs/NOTIFICATIONS.md`,
  `docs/GMAIL_SETUP.md`, `docs/MULTI_USER.md`.

### Changed
- The dashboard funnel counts each job from its **own current state** (not only
  from recorded events), so stage totals reflect the full history rather than
  undercounting jobs with no event rows.
- Inbox triage always includes active jobs (screening/interviewing/offer) as
  candidates regardless of the recency cap, so a late reply is never missed.

### Security
- The inbox agent never writes email, attachments, or message content to disk
  (a wiped download jail enforces it), and its subprocess timeout is
  configurable.
- Terminal outcomes are protected and triage candidates are capped.

## [0.1.0] — 2026-09-10

First public release: the full `discover → enrich → score → tailor →
cover-letter → apply` pipeline on local SQLite, the immutable-facts résumé
guarantee, apply-stage safety guards (dry-run first, rate limits, daily cap
under a hard ceiling, CAPTCHAs routed to a human), the provenance attestation,
and the review dashboard. Shipped with NOTICE, CONTRIBUTING, Code of Conduct,
issue/PR templates, and a privacy-first gitignore.

[Unreleased]: https://github.com/shardulkulkarni14/honestapply/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/shardulkulkarni14/honestapply/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/shardulkulkarni14/honestapply/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/shardulkulkarni14/honestapply/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/shardulkulkarni14/honestapply/releases/tag/v0.1.0
