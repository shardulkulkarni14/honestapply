# Multi-user (Phase 3) — architecture & staged plan

**Status: DESIGN / foundation.** honestapply ships single-user and local. This maps
the path to multi-user and is the blueprint the staged build follows. The
data-isolation pieces are **security-critical** (a missed scope check could leak
one user's PII/applications to another), so they are built in verified stages, not
in one drop. This document is deliberately the first deliverable.

## The model: workspace-per-user (physical isolation) — NOT shared-DB rows

Each user gets their own **workspace = a data directory** that holds everything:

```
workspaces/<user_id>/
  honestapply.db          # their jobs/applications/events
  config/                 # profile.json, resume YAMLs, answers.yaml
  vault/                  # their secrets (site passwords), own encryption key
  browser-profile/        # their logged-in browser sessions
  outputs/                # their generated CVs / cover letters / screenshots
```

**Why physical isolation over a shared DB with `user_id` on every row:** honestapply
is built around a per-install SQLite DB + config dir + vault + browser profile. A
shared multi-tenant DB means every query needs the right `WHERE user_id = …`; one
missed clause leaks another person's job history, CV, or contact details. With a
workspace per user there is **no row-level leak surface** — isolation is a
filesystem boundary, which is far harder to get wrong and fits the existing design.

**The isolation primitive already mostly exists.** honestapply already routes its
state through configurable paths/env:
- DB: `HONESTAPPLY_DB_PATH`
- config dir: `project_root()/config` (PATHS)
- vault: `HONESTAPPLY_VAULT_DIR` / `HONESTAPPLY_VAULT_KEY`
- browser profile: `HONESTAPPLY_BROWSER_PROFILE`
- outputs: `data/outputs`

So a workspace is "point all of these at `workspaces/<user_id>/…`". Phase 3 is
mostly **auth + a router that sets this context per request/run**, not a rewrite.

## Components (staged — each shippable + testable on its own)

**3a — Auth.** A login layer (email+password with hashed+salted passwords, or
OAuth/"Sign in with Google"). Server-side sessions (signed cookie). Every API
endpoint and page requires a valid session. No endpoint is reachable
unauthenticated except login itself.

**3b — Workspace routing.** Resolve the authenticated user → their workspace dir,
and run every request/pipeline-run *within* that workspace's paths. The current
single install becomes "the default workspace", so existing data is preserved. The
dashboard's module-global DB/config load (today) is refactored to **per-request
workspace context** — this refactor is the core of 3b.

**3c — Workspace manager + onboarding.** Create / list / delete a workspace per
user; the guided onboarding (upload CV → build profile+resume, set search
preferences, connect Gmail) writes into that user's workspace. (Pairs with the
onboarding work flagged in the Phase-1 wrap-up — it's the real non-tech unlock.)

**3d — Per-user run isolation.** The Phase-2 runner becomes **per-workspace**: one
active run per user, a global concurrency cap across users, and the apply stage
stays serialized per user (rate limits + browser profile are per-workspace). A
real job queue (RQ/Celery/Arq) replaces the in-process runner once there is more
than a handful of users.

**3e — Hosting.** Containerize; run behind HTTPS with the auth above; per-workspace
backups; resource limits. Bind moves off `127.0.0.1` **only** once auth (3a) is in.

## Security invariants (non-negotiable)

- **No cross-workspace access, ever.** A request for user A can only ever touch
  `workspaces/A/…`. Enforced by the router, verified by tests that assert a user
  cannot read another workspace's DB/files.
- **Auth on every endpoint.** Default-deny; the login route is the only exception.
- **Secrets never cross workspaces.** Each workspace has its own vault + key; the
  key is never shared. The hosted-browser PII caveat (see the apply docs) applies
  per user and must be surfaced to each.
- **The apply safety rails stay per workspace** (dry-run-first, caps, rate limits,
  confirm-before-real, CAPTCHA→needs_human).

## Migration path (no big-bang)

1. Ship single-user (today). 
2. Add **auth in front** of the existing app (3a) — the one install = one user.
3. Introduce **workspace routing** (3b); the existing data becomes the default
   workspace — zero data migration.
4. Add **workspace creation + onboarding** (3c) so new users self-serve.
5. Add **per-user run isolation** (3d) and **hosting** (3e).

## Not yet built (and why)

Auth, the workspace router, the manager, per-user run isolation, and hosting are
the staged build above. They are intentionally **not** scaffolded half-way here:
partial auth or partial isolation is worse than none (it invites a false sense of
safety and a data-leak). This blueprint exists so each stage is built complete and
verified. **Recommended first stage to implement: 3a (auth) + 3b (workspace
routing)** — together they turn the current app into a safe single-tenant-per-login
system, which is the real unlock; 3c–3e follow.
