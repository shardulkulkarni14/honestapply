# Gmail inbox sync — setup

`honestapply inbox` reads recruiter / ATS emails from your Gmail and updates the
status of jobs you've applied to (interview invite → `interviewing`, rejection →
`rejected`, …). It's **optional**.

honestapply is an agentic tool, so — just like the apply stage drives a browser
over **Playwright MCP** — the inbox reads Gmail over **MCP tool calls**. A short
`claude` agent is spawned with a **local Gmail MCP server** available (declared in
`.mcp.json`); it searches and reads your mail via MCP, and honestapply applies its
own guards to the result and writes the status changes.

**The connection belongs to honestapply, not your Claude account.** The local
server ([`@klodr/gmail-mcp`](https://github.com/rdanco-nri/gmail-mcp)) authenticates
with *your own* Google OAuth client and stores its token in a local file
(`~/.gmail-mcp/credentials.json`, `chmod 600`). So it keeps working even if your
Claude login changes. It's authorized **read-only**: the server filters its tool
list to the granted scope, so no send/modify tools are ever exposed to the agent.

## 0. Prerequisite: Node.js 22+

The server runs via `npx`, and `@klodr/gmail-mcp` needs **Node 22+**:

```bash
node --version          # must be >= 22
brew install node@22    # if you're below 22 (then follow brew's PATH hint)
```

## 1. Create a Google OAuth client (one-time)

1. In the [Google Cloud Console](https://console.cloud.google.com/), create or
   pick a project and enable the **Gmail API**:
   ```bash
   gcloud services enable gmail.googleapis.com --project=PROJECT_ID
   ```
   (or **APIs & Services → Library → Gmail API → Enable**).
2. **OAuth consent screen / Audience:** User type **External**; add your own
   Google address as a **Test user** (keeping the app in "Testing" is fine for
   personal use — no Google review; you click through an "unverified app" notice
   once).
3. **Data Access / Scopes:** add `https://www.googleapis.com/auth/gmail.readonly`.
4. **Credentials → Create credentials → OAuth client ID:**
   - **Desktop app** (simplest — no redirect URI needed), **or**
   - **Web application** with authorized redirect URI
     `http://localhost:3000/oauth2callback`.
5. **Download the client JSON** and save it as:
   ```
   ~/.gmail-mcp/gcp-oauth.keys.json
   ```
   ```bash
   mkdir -p ~/.gmail-mcp
   mv ~/Downloads/client_secret_*.json ~/.gmail-mcp/gcp-oauth.keys.json
   ```

## 2. Connect

```bash
honestapply gmail-connect
```

This runs the server's one-time auth (`npx @klodr/gmail-mcp auth
--scopes=gmail.readonly`): a browser opens, you sign in and approve, and the token
is written to `~/.gmail-mcp/credentials.json`. You only do this once — it refreshes
itself.

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

- **honestapply-owned & account-independent.** The OAuth client and token are
  yours and live on this machine (`~/.gmail-mcp/`). Nothing about this depends on
  your Claude account — only the LLM *inference* the agent uses does, and that's
  easily re-pointed (set an API key in `.env`, or re-login to Claude Code).
- **Read-only, enforced by the server.** With `gmail.readonly` the server exposes
  no send/modify tools at all — not just "we won't use them."
- **Honest matching.** A job only changes status on a confident, clear match that
  independently names the company, attributed to `source='email'` in `job_events`,
  and never backwards. Email bodies are treated as untrusted — the agent never
  follows instructions found inside a message.
- **Offline/dev.** `HONESTAPPLY_INBOX_MOCK=1 honestapply inbox` runs without
  spawning the agent (finds nothing). Override the agent model with
  `HONESTAPPLY_INBOX_MODEL` (default `claude-opus-5`).
- **Switching servers.** honestapply is server-agnostic. To use a different Gmail
  MCP server, change the `gmail` entry in `.mcp.json` and the `gmail_mcp_package` /
  `gmail_mcp_config_dir` settings to match.
