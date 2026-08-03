"""Pick the next batch of candidate job IDs for one country.

Feeds `scripts/batch_drive.py`, which drives exactly these IDs to COVERED.
Splitting by country lets one cycle target e.g. 10 Germany + 5 India without
letting the larger backlog crowd out the smaller market.

    python scripts/pick_candidates.py --country DE --limit 30
    python scripts/pick_candidates.py --country IN --limit 15

Prints a comma-separated ID list on stdout (empty if nothing is eligible).

Employers that have already sent a rejection are excluded: re-applying to a
company that passed on the profile wastes a slot against the daily cap.
"""
from __future__ import annotations

import argparse
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import select

from honestapply.db.models import Job, Status
from honestapply.db.session import session_scope
from honestapply.stages.prefilter import DEAD_HOST_MARKERS

# Locations that mark a posting as Indian. Kept deliberately broad — Indian
# postings often name only the city ("Pune, Maharashtra") with no country.
_IN = re.compile(
    r"\b(india|bengaluru|bangalore|pune|hyderabad|mumbai|chennai|"
    r"delhi|gurgaon|gurugram|noida|kolkata|ahmedabad|maharashtra|karnataka|"
    r"telangana|haryana|uttar pradesh|tamil nadu)\b",
    re.IGNORECASE,
)
# India ranking tiers. Postings in the configured priority city rank first, then
# remote-into-India roles: companies hiring remotely into India tend to pay a band
# above ordinary local roles. Everything else still qualifies — this only decides
# who gets the limited per-cycle slots first.
_IN_PRIORITY = re.compile(r"\b(hyderabad|telangana|secunderabad)\b", re.IGNORECASE)
_IN_REMOTE = re.compile(r"\bremote\b", re.IGNORECASE)

# Europe beyond Germany. Ranked below the home market: an EU role in another
# member state usually still needs its own permit rather than being automatic, so
# these are actionable but not equivalent to a local posting.
_EU_OTHER = re.compile(
    r"\b(austria|vienna|wien|graz|linz|salzburg|innsbruck|"
    r"netherlands|amsterdam|rotterdam|utrecht|eindhoven|the hague|den haag|delft|"
    r"switzerland|zurich|zürich|geneva|genève|basel|bern|lausanne|zug|"
    r"france|paris|lyon|toulouse|grenoble|nice|bordeaux|lille|nantes|"
    r"belgium|brussels|bruxelles|antwerp|ghent|leuven|"
    r"ireland|dublin|cork|galway|"
    r"spain|madrid|barcelona|valencia|seville|malaga|bilbao|"
    r"portugal|lisbon|lisboa|porto|braga|"
    r"italy|milan|milano|rome|roma|turin|torino|bologna|florence|"
    r"sweden|stockholm|gothenburg|göteborg|malmo|malmö|lund|"
    r"norway|oslo|bergen|trondheim|"
    r"denmark|copenhagen|københavn|aarhus|"
    r"finland|helsinki|espoo|tampere|oulu|"
    r"poland|warsaw|warszawa|krakow|kraków|wroclaw|wrocław|gdansk|poznan|"
    r"czech|czechia|prague|praha|brno|ostrava|"
    r"hungary|budapest|debrecen|"
    r"romania|bucharest|bucuresti|cluj|timisoara|iasi|"
    r"bulgaria|sofia|plovdiv|"
    r"greece|athens|thessaloniki|"
    r"slovakia|bratislava|kosice|"
    r"slovenia|ljubljana|croatia|zagreb|"
    r"estonia|tallinn|tartu|latvia|riga|lithuania|vilnius|kaunas|"
    r"luxembourg|malta|valletta|cyprus|nicosia|iceland|reykjavik|"
    r"\beurope\b|\beu\b|emea|dach|benelux|nordics|nordic)\b",
    re.IGNORECASE,
)

_DE = re.compile(
    r"(\b(germany|deutschland|munich|münchen|ingolstadt|berlin|hamburg|frankfurt|"
    r"cologne|köln|düsseldorf|duesseldorf|stuttgart|nuremberg|nürnberg|augsburg|"
    r"dortmund|essen|leipzig|hannover|hanover|mainz|walldorf|karlsruhe|bremen|"
    r"dresden|bonn|freiburg|heidelberg|mannheim|aachen|münster|muenster|erlangen|"
    r"regensburg|ulm|braunschweig|wolfsburg|jena|potsdam|kiel|darmstadt|kassel|"
    r"bielefeld|wiesbaden|bochum|würzburg|wuerzburg|osnabrück|paderborn|lübeck)\b"
    # Boards commonly render the country as a trailing ISO code: "Freiburg, BW, DE".
    r"|,\s*DE\s*$|\(germany\))",
    re.IGNORECASE,
)

# Employers to never apply to, regardless of score. Kept empty here on purpose:
# a personal do-not-apply list belongs in the gitignored config/employers.yaml,
# not in version control.
_HARD_EXCLUDE: set[str] = set()

# Staffing agencies and body-shops. They post other companies' roles, so the JD
# rarely matches the real employer and the application goes to a recruiter
# pipeline rather than a hiring team.
_AGENCY = re.compile(
    r"\b(randstad|hays|adecco|manpower|michael page|robert half|hunter|"
    r"gulp|brunel|ferchau|solcom|etengo|amoria|darwin recruitment|"
    r"personalberatung|zeitarbeit|staffing|recruit(ing|ment) (agency|solutions))\b",
    re.IGNORECASE,
)

# Placeholder company names that should never reach an application form.
_BAD_COMPANY = {"nan", "none", "null", "n/a", "-", ""}

# Title relevance. `prefilter` is deliberately conservative — it drops only
# obvious junk, so plenty of irrelevant-but-professional roles ("Senior Manager
# Tax") survive it. Every one of those costs four LLM calls before the score
# gate rejects it, so rank on-profile titles first rather than purely by date.
# Tier 2 — the actual profile: AI/ML/GenAI roles. These are what the score gate
# rewards, so they must outrank everything else.
_AI_TITLE = re.compile(
    r"(machine learning|\bml\b|\bmlops\b|artificial intelligence|\bai\b|genai|"
    r"generative ai|\bllm\b|\bnlp\b|deep learning|data scien|applied scien|"
    r"ai architect|research engineer|research scientist|"
    # AI labs use their own title conventions — they post
    # "Member of Technical Staff — Pretraining / Post-Training / VLM", which
    # carries no "AI"/"ML" token and would otherwise rank as tier 0.
    r"member of technical staff|pre-?training|post-?training|\bvlm\b|"
    r"multimodal|diffusion|computer vision|forward deployed)",
    re.IGNORECASE,
)

# Tier 1 — adjacent software roles. Still worth applying to, but they score far
# lower against an AI-specialist profile. Keeping them in a SEPARATE tier matters:
# when they shared a tier with AI titles, generic "Software Engineer" postings
# (of which the backlog holds thousands) crowded out the few hundred real AI
# roles, and a whole run scored 71/88 candidates at 1/10.
_ADJACENT_TITLE = re.compile(
    r"(software engineer|backend|back-end|full.?stack|python|platform engineer|"
    r"solutions architect|solution architect|developer)",
    re.IGNORECASE,
)


def _is_live(url: str) -> bool:
    """True if the posting still exists.

    Worth the HTTP round-trip: a dead posting otherwise costs four LLM calls
    (enrich/score/tailor/cover) and a browser session before the apply stage
    discovers the 404. Observed on 2026-07-31, five of six prepared applications
    failed this way — expired Greenhouse/Lever/careers-page listings.

    Errs toward keeping: only an explicit 404/410, or a redirect to a board
    index/error page, counts as dead. Network flakiness must not drop good jobs.
    """
    if not url:
        return True
    try:
        r = httpx.get(url, timeout=8.0, follow_redirects=True)
    except Exception:  # noqa: BLE001 — network trouble is not evidence of death
        return True

    if r.status_code in (404, 410):
        return False
    # Boards bounce expired postings to a listing page or ?error=true.
    final = str(r.url).lower()
    if "error=true" in final or final.rstrip("/").endswith(("/jobs", "/careers")):
        return False
    return True


def _filter_live(rows: list, limit: int) -> list:
    """Keep the first *limit* rows whose posting is still reachable."""
    kept: list = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        # Probe a padded window so one dead batch doesn't starve the result.
        window = rows[: limit * 3]
        for row, alive in zip(window, pool.map(lambda r: _is_live(r[1]), window)):
            if alive:
                kept.append(row)
            if len(kept) >= limit:
                break
    return kept


def _title_rank(title: str) -> int:
    """2 = core AI/ML role, 1 = adjacent software role, 0 = everything else."""
    if _AI_TITLE.search(title or ""):
        return 2
    if _ADJACENT_TITLE.search(title or ""):
        return 1
    return 0

# Statuses that still have somewhere to go in the pipeline.
_OPEN = [Status.DISCOVERED, Status.ENRICHED, Status.SCORED, Status.TAILORED]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", choices=["DE", "IN", "EU"], required=True)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument(
        "--remote-only",
        action="store_true",
        help="Keep only postings whose location says remote (used for India).",
    )
    ap.add_argument(
        "--established-only",
        action="store_true",
        help="Skip early-stage startups: keep employers that are either on the "
             "curated big/medium list or post at least --min-postings roles.",
    )
    ap.add_argument(
        "--min-postings",
        type=int,
        default=8,
        help="Size proxy for --established-only: an employer with this many "
             "postings in the DB is not an early-stage startup (default 8).",
    )
    ap.add_argument(
        "--companies-file",
        default="config/big_medium_employers.txt",
        help="Curated big/medium employer names (one per line, # comments).",
    )
    args = ap.parse_args()

    known_big: list[str] = []
    if args.established_only:
        try:
            with open(args.companies_file) as fh:
                known_big = [
                    ln.strip().lower()
                    for ln in fh
                    if ln.strip() and not ln.startswith("#")
                ]
        except OSError:
            known_big = []

    if args.country == "IN":
        pattern = _IN
    elif args.country == "EU":
        # Germany plus the rest of Europe.
        pattern = re.compile(f"({_DE.pattern})|({_EU_OTHER.pattern})", re.IGNORECASE)
    else:
        pattern = _DE

    with session_scope() as s:
        # Companies that have ever rejected us — skip every posting of theirs.
        # "rejected" is written by the Gmail rejection sweep, not by a pipeline
        # stage, so it has no Status member — match the raw column value.
        rejected = {
            (c or "").strip().lower()
            for c in s.scalars(
                select(Job.company).where(Job.status == "rejected")
            ).all()
        }
        blocked = rejected | _HARD_EXCLUDE

        rows = s.scalars(
            select(Job).where(Job.status.in_(_OPEN))
        ).all()

        # Size proxy for --established-only. There is no company-size field, so
        # use how many roles the employer has on file: an early-stage startup
        # posts a handful, an established company posts dozens. The curated list
        # overrides it for large firms whose board we have only partly sourced.
        posting_counts: dict[str, int] = {}
        if args.established_only:
            for j in rows:
                key = (j.company or "").strip().lower()
                posting_counts[key] = posting_counts.get(key, 0) + 1

        def _is_established(company: str) -> bool:
            key = (company or "").strip().lower()
            if not key:
                return False
            if any(big in key for big in known_big):
                return True
            return posting_counts.get(key, 0) >= args.min_postings

        picked: list[tuple[int, int, str]] = []
        for j in rows:
            company = (j.company or "").strip()
            if company.lower() in blocked or company.lower() in _BAD_COMPANY:
                continue
            if _AGENCY.search(company):
                continue

            # Skip hosts the apply stage cannot submit to. `prefilter` already
            # drops these, but only for DISCOVERED jobs — anything ENRICHED by an
            # earlier run predates that gate and would otherwise cost four LLM
            # calls (enrich/score/tailor/cover) before apply rejects it as
            # "Indeed posting" or "LinkedIn Easy Apply disabled".
            if any(marker in (j.url or "").lower() for marker in DEAD_HOST_MARKERS):
                continue

            # Remote-only mode (India): the user works from Germany, so an
            # onsite Indian posting is not actionable without relocating.
            if args.remote_only and not _IN_REMOTE.search(j.location or ""):
                continue

            if args.established_only and not _is_established(j.company or ""):
                continue
            if not pattern.search(j.location or ""):
                # Remote postings carry no country; only claim them for DE so
                # they aren't double-counted into the India batch.
                if args.country == "DE" and not (j.location or "").strip():
                    pass
                else:
                    continue
            on_profile = _title_rank(j.title or "")
            # India preference order, applied as a *ranking* rather than a filter
            # so a thin remote pool never starves the cycle — onsite still fills
            # the target once better options run out:
            #   3 remote-into-India — employers hiring remotely into India pay the
            #     highest band, so these rank first.
            #   2 the configured priority city — preferred if relocating.
            #   1 other onsite India — actionable only with relocation, and
            #     typically a lower band.
            # Neutral (0) for Germany.
            loc = j.location or ""
            city_rank = 0
            if args.country == "IN":
                if _IN_REMOTE.search(loc):
                    city_rank = 3
                elif _IN_PRIORITY.search(loc):
                    city_rank = 2
                else:
                    city_rank = 1
            picked.append(
                (on_profile, city_rank, j.score or 0, j.discovered_at.isoformat(),
                 j.id, j.url or "")
            )

        # On-profile titles first, then preferred city, then highest known score,
        # then freshest. Lower-ranked roles stay in the list (they sort last) so
        # a thin market still yields candidates rather than returning nothing.
        picked.sort(reverse=True)

        # Drop postings that have already expired before any LLM cost is spent.
        rows = [(t[4], t[5]) for t in picked]
        ids = [str(jid) for jid, _ in _filter_live(rows, args.limit)]

    print(",".join(ids))


if __name__ == "__main__":
    main()
