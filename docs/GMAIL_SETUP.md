# Gmail inbox sync — setup

`honestapply inbox` reads recruiter / ATS emails from your Gmail and updates the
status of jobs you've applied to (interview invite → `interviewing`, rejection →
`rejected`, and so on). It's **optional**.

honestapply is an agentic tool, so — just like the apply stage drives a browser
over **Playwright MCP** — the inbox reads Gmail over **MCP tool calls**. A short
`claude` agent is spawned with a **Gmail MCP server** available (declared in
`.mcp.json`); it searches and reads your mail via MCP, and honestapply applies its
own guards to the result and writes the status changes. There is **no Gmail API
client and no stored token** inside honestapply — the MCP client (Claude Code)
owns the connection and its OAuth token. So there are **no extra Python
dependencies** to install.

The default server is Google's official remote Gmail MCP server
(`https://gmailmcp.googleapis.com/mcp/v1`). It can search/read threads, manage
labels and create drafts — but it has **no send tool**, which is exactly what we
want: the inbox only ever reads, and a human stays in the loop for any reply.

## 1. One-time Google Cloud setup

Google's Gmail MCP server is in developer preview and talks OAuth 2.0 against
*your own* Google Cloud project.

1. In the [Google Cloud Console](https://console.cloud.google.com/), create (or
   pick) a project and enable the two services:
   ```bash
   gcloud services enable gmail.googleapis.com --project=PROJECT_ID
   gcloud services enable gmailmcp.googleapis.com --project=PROJECT_ID
   ```
2. **Google Auth Platform → Branding / OAuth consent screen:** choose
   **External**, fill the required fields, and add your own Google address as a
   **Test user** (keeping the app in "Testing" is fine for personal use — no
   Google review needed; you click through an "unverified app" notice once).
3. Add the scopes the server uses:
   - `https://www.googleapis.com/auth/gmail.readonly` (reading — all the sync needs)
   - `https://www.googleapis.com/auth/gmail.compose` (only for the future
     opt-in draft-reply feature)
4. **Credentials → Create credentials → OAuth client ID.** Note the **client ID**
   and **client secret** — you'll give them to Claude Code as the MCP client.
   (honestapply never sees or stores them.)

> Prefer not to run your own Google Cloud project? The inbox is **server-agnostic**
> — point the `gmail` entry in `.mcp.json` at any Gmail MCP server (for example a
> local `npx` one) that exposes `search_threads` / `get_thread`, and nothing else
> changes. honestapply only spawns the agent; the agent uses whatever Gmail MCP
> is configured.

## 2. Connect

```bash
honestapply gmail-connect
```

This opens a Claude Code session. Type `/mcp`, choose **gmail → Authenticate**,
and approve access in the browser (enter the client ID/secret when prompted). The
token is stored by Claude Code, outside this repo. You only do this once — it
refreshes itself.

## 3. Sync

```bash
honestapply inbox            # scan the last 30 days, update statuses
honestapply inbox --days 7   # or a shorter window
```

Check what it did:

```bash
honestapply status
honestapply doctor           # shows "Gmail MCP (inbox): connected"
```

## Notes

- **Honest by construction.** A job only changes status on a **confident, clear
  match that independently names the company** (a sanity check against the agent
  attributing an email to the wrong job), the change is attributed to
  `source='email'` in `job_events`, and it never moves a job backwards.
- **Untrusted content.** The agent is instructed that email bodies are untrusted
  data — it classifies what a message *is* and never follows instructions found
  inside one, and it never sends, replies to, labels, or modifies anything.
- **No secrets in the repo.** honestapply stores no Gmail credentials. The OAuth
  client ID/secret and token live with the MCP client (Claude Code).
- **Offline/dev.** Set `HONESTAPPLY_INBOX_MOCK=1` to run `inbox` without spawning
  the agent (it finds nothing) — handy for smoke tests. Override the agent model
  with `HONESTAPPLY_INBOX_MODEL` (default `claude-opus-5`).
