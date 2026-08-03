"""Stage 1.5 — cheap, no-LLM relevance pre-filter.

The LLM stages (enrich → score → tailor → cover) are expensive: scoring drains
the *entire* DISCOVERED/ENRICHED backlog one `claude_cli` call at a time. Most of
that backlog is noise — bus "station agents", interns/Werkstudenten, warehouse
roles, and postings on dead hosts (Indeed expires, LinkedIn ban-guards apply).

`run_prefilter()` scans DISCOVERED jobs and routes the obvious non-fits to
SKIPPED_LOW_FIT with a `prefilter:` reason — using only string heuristics, no
network and no LLM. It is deliberately *conservative*: it drops only clear junk
(dead hosts, plainly non-engineering titles) and leaves every plausible role for
the real LLM scoring gate to judge. This shrinks the input the LLM stages see by
a large factor without risking good roles.

Entry point:
    run_prefilter(ids: list[int] | None = None, limit: int | None = None) -> dict
        Returns {"scanned": int, "dropped": int, "kept": int}.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.logging_setup import get_logger

log = get_logger(__name__)

# Hosts that do not yield real submissions: Indeed postings expire fast and route
# to needs_human; LinkedIn is disabled by the apply ban-guard.
DEAD_HOST_MARKERS = ("indeed.", "linkedin.")

# Titles that are clearly outside an AI/ML/software-engineering profile. Matched
# as whole words/phrases to avoid clobbering legitimate roles (e.g. "Solutions
# Engineer", "Sales Engineering Lead" are NOT dropped — only obvious non-eng work).
_IRRELEVANT_TITLE = re.compile(
    r"\b("
    r"intern|internship|werkstudent|working student|praktikum|praktikant|"
    r"ausbildung|azubi|trainee program|dual stud|"
    # German student-programme wording the English patterns miss — 216 such
    # postings were reaching the LLM stages ("Duales Studium Informatik",
    # "Studentische Hilfskraft", "Student Helper").
    r"duales studium|duale[sn]? hochschul|studentische|studentenjob|"
    r"student helper|schüler|schueler|bachelorand|masterand|diplomand|"
    r"master'?s? thesis|masterarbeit|bachelorarbeit|abschlussarbeit|"
    r"station agent|booking agent|call center|call agent|customer service|"
    r"customer support|kundenservice|kundenbetreuung|d'escale|"
    r"warehouse|lagerist|lager\b|kommissionier|fahrer\b|driver\b|kurier|"
    r"reinigung|cleaner|cleaning|security guard|monteur|mechaniker|"
    r"verk[äa]ufer|cashier|kassierer|receptionist|empfang|"
    r"pflege|nurse|koch\b|k[üu]che|barista|kellner|waiter"
    r")\b",
    re.IGNORECASE,
)

# Role types that tend to mismatch an *applied* GenAI/LLM/agents profile:
# deep-systems platform/infra/SDK/perf work, pre-training/model-training research,
# forward-deployed/FDE delivery, and frontend-heavy "product/builder" engineering.
# Provided as optional steering for profiles that don't convert on these.
# Guarded so applied-AI titles (AI/ML/GenAI/LLM Engineer, Data/Applied Scientist)
# are NEVER caught — the mismatch keyword must be the role's own noun.
_AI_ROLE = re.compile(
    r"\b(ai|a\.i\.|ml|genai|gen ai|llm|nlp|machine learning|deep learning|"
    r"data scien|applied scien|generative)\b",
    re.IGNORECASE,
)
# Hard mismatch: drop even when an AI keyword is present — the *nature* of the
# role (research pre-training, forward-deployed delivery, pure ops) is the misfit.
_HARD_MISMATCH = re.compile(
    r"\b(pre-?training|model training|forward deployed|fde|"
    r"site reliability|sre|devops)\b",
    re.IGNORECASE,
)
# Soft mismatch: drop only when the title carries NO applied-AI signal — generic
# SWE-infra/frontend roles.
_SOFT_MISMATCH = re.compile(
    r"\b(platform engineer|infrastructure engineer|cloud (infrastructure )?engineer|"
    r"performance engineer|sdk|builder|"
    r"frontend engineer|front-end engineer|backend engineer|full[- ]?stack engineer)\b",
    re.IGNORECASE,
)


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def is_relevant(title: str, url: str = "", location: str = "") -> tuple[bool, str]:
    """Cheap relevance verdict. Returns (keep, reason_if_dropped).

    Conservative: only returns False on strong negative signals so the LLM gate
    still decides every borderline case.
    """
    host = _host(url)
    if any(marker in host for marker in DEAD_HOST_MARKERS):
        return False, "dead host (Indeed expires / LinkedIn ban-guard)"
    if title and _IRRELEVANT_TITLE.search(title):
        return False, "title not engineering/AI-relevant"
    # NOTE: the title-mismatch drops (_HARD_MISMATCH / _SOFT_MISMATCH) are
    # deliberately NOT applied. Steering away from pre-training/FDE/platform
    # titles also silently removed every role at AI-first labs, whose postings
    # routinely carry exactly those words (e.g. "Member of Technical Staff —
    # Pretraining/Post-Training/VLM"). Letting the LLM fit-score gate judge them
    # on merit is more accurate than excluding them by title. The patterns are
    # kept above so the behaviour can be restored by re-adding the two checks.
    return True, ""


# Every status that still sits *before* the apply stage. Prefilter used to scan
# only DISCOVERED, so a job enriched by an earlier run kept its dead host for
# good: it was never re-examined, and went on to cost four LLM calls before the
# apply stage rejected it as "Indeed posting" / "LinkedIn Easy Apply disabled".
PRE_APPLY_STATUSES = (
    Status.DISCOVERED,
    Status.ENRICHED,
    Status.SCORED,
    Status.TAILORED,
)


def run_prefilter(ids: list[int] | None = None, limit: int | None = None) -> dict:
    """Route clearly-irrelevant pre-apply jobs to SKIPPED_LOW_FIT (no LLM, no network)."""
    scanned = dropped = 0
    with session_scope() as session:
        query = session.query(Job).filter(Job.status.in_(PRE_APPLY_STATUSES))
        if ids:
            query = query.filter(Job.id.in_(ids))
        if limit:
            query = query.limit(limit)
        for job in query.all():
            scanned += 1
            keep, reason = is_relevant(job.title or "", job.url or "", job.location or "")
            if not keep:
                job.status = Status.SKIPPED_LOW_FIT
                job.status_reason = f"prefilter: {reason}"
                dropped += 1
    kept = scanned - dropped
    log.info("prefilter.done", scanned=scanned, dropped=dropped, kept=kept)
    return {"scanned": scanned, "dropped": dropped, "kept": kept}
