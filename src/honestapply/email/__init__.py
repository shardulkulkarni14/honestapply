"""Gmail inbox sync for honestapply.

`gmail.py` is a thin, lazily-imported wrapper over the Gmail API (OAuth connect,
list/fetch messages, optional draft creation). `sync.py` is the `inbox` stage:
it reads recent mail, matches it to the jobs you have applied to, classifies it
with the LLM, and records the outcome — moving a job's status (via
``transition(source="email")``) only on a confident, clear match.

Everything here is optional: it imports the Google client libraries only when
used, so the rest of honestapply runs without the `[gmail]` extra installed.
"""
