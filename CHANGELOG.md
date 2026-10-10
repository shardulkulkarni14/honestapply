# Changelog

All notable changes to honestapply are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (pre-1.0: minor
versions may still change behaviour).

## [Unreleased]

Merged to `main`, not yet released — the next release (0.3.0) will ship these
together with the deterministic ATS apply fast-path.

### Added
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

### Changed
- The dashboard funnel, CLI status counts, and analytics all exclude archived jobs.

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

[Unreleased]: https://github.com/shardulkulkarni14/honestapply/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/shardulkulkarni14/honestapply/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/shardulkulkarni14/honestapply/releases/tag/v0.1.0
