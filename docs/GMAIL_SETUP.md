# Gmail inbox sync — setup

`honestapply inbox` reads recruiter / ATS emails from your Gmail and updates the
status of jobs you've applied to (interview invite → `interviewing`, rejection →
`rejected`, and so on). It's **optional** and **local-first**: your token and
email stay on your machine, never in git and never on a server.

Because honestapply runs on *your* machine, Google requires a one-time OAuth
setup (there's no hosted app to click "Allow" against, the way a verified SaaS
product has). After this ~3-minute setup, connecting is a single command.

## 1. Install the extra

```bash
pip install -e ".[gmail]"
```

## 2. Create a Google OAuth client (one-time)

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create (or pick) a project.
2. **APIs & Services → Library →** enable the **Gmail API**.
3. **APIs & Services → OAuth consent screen:** choose **External**, fill the
   required fields, and add **your own Google address as a Test user**
   (keeping the app in "Testing" is fine for personal use — no Google review
   needed; you'll just click through an "unverified app" notice once).
4. Add the scope **`https://www.googleapis.com/auth/gmail.readonly`** (read-only
   — all the inbox sync needs). Add `gmail.compose` too *only* if you want the
   optional draft-reply feature.
5. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Application type: Desktop app.** Download the JSON.
6. Save it as **`config/gmail_credentials.json`** in your honestapply checkout.
   (It's gitignored — it never gets committed.)

## 3. Connect

```bash
honestapply gmail-connect
```

A browser opens; approve access. A token is saved to
`~/.honestapply/gmail_token.json` (outside the repo, `chmod 600`). You won't need
to do this again — it refreshes itself.

## 4. Sync

```bash
honestapply inbox            # scan the last 30 days, update statuses
honestapply inbox --days 7   # or a shorter window
```

Check what it did:

```bash
honestapply status
honestapply doctor           # shows "Gmail (inbox): connected"
```

## Notes

- **Least privilege:** the default scope is read-only. A job only changes status
  on a confident, clear match, it's attributed to `source='email'` in
  `job_events`, and it never moves a job backwards.
- **Untrusted content:** email bodies are treated as untrusted data — the
  classifier never follows instructions contained in an email.
- **True one-click (maintainers):** to remove step 2 for end users, the project
  would publish its OAuth app and pass Google's verification for the restricted
  Gmail scope. The code already supports a shared client — drop it in at
  `config/gmail_credentials.json` and users skip straight to `gmail-connect`.
