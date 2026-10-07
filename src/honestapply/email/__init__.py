"""Gmail inbox sync for honestapply — built on MCP, not the Gmail REST API.

honestapply is an agentic tool, so the inbox works like the apply stage: rather
than calling the Gmail API from Python, it spawns a ``claude`` agent that reads
mail through a **Gmail MCP server** (configured in ``.mcp.json`` beside
Playwright) via MCP *tool calls*.

- ``agent.py`` runs that triage agent (``claude -p``) and parses its single
  ``<<<RESULT>>>`` JSON block — the same contract the browser-apply agent uses.
- ``sync.py`` is the ``inbox`` stage: it shortlists the jobs you've applied to,
  runs the agent over recent mail, then applies honestapply's own guards
  (confidence floor, company-mention check, forward-only transitions,
  ``source='email'`` attribution, dedup) to the agent's verdicts and records them.

No Google Python client libraries are required — the Gmail access lives entirely
in the MCP server the spawned ``claude`` talks to.
"""
