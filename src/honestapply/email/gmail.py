"""Thin Gmail API wrapper: OAuth connect + read + (optional) draft.

Google's client libraries are heavy and optional, so they are imported inside
the functions that need them — importing this module never pulls them in. A
missing library or a not-yet-connected account raises :class:`GmailUnavailable`
with a one-line fix, so the CLI can print guidance instead of a stack trace.

Token lifecycle follows Google's documented pattern: load the saved token, use
it; refresh it silently when expired; and only fall back to the interactive
browser consent flow (``InstalledAppFlow.run_local_server``) when there is no
usable token and the caller asked for it. The token is written to
``settings.gmail_token_file`` (outside the repo, in ~/.honestapply).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr  # stdlib `email`, not this package
from typing import TYPE_CHECKING, Any

from honestapply.config import Settings, get_settings
from honestapply.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.oauth2.credentials import Credentials

logger = get_logger(__name__)


class GmailUnavailable(RuntimeError):
    """Gmail can't be used yet — deps missing, credentials missing, or not
    connected. The message tells the user exactly how to fix it."""


@dataclass
class ParsedEmail:
    """The fields of one message the classifier and store care about."""

    msg_id: str
    thread_id: str
    sender: str
    sender_domain: str
    subject: str
    snippet: str
    body: str
    received_at: datetime | None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _require_libs():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise GmailUnavailable(
            "Gmail support is not installed. Run:  pip install -e \".[gmail]\""
        ) from exc
    return Request, Credentials, InstalledAppFlow, build


def connect(settings: Settings | None = None, *, interactive: bool = False) -> "Credentials":
    """Return valid Gmail credentials, refreshing or (if *interactive*) running
    the browser consent flow as needed. Raises :class:`GmailUnavailable` when a
    non-interactive call has no usable token, or credentials are missing."""
    settings = settings or get_settings()
    Request, Credentials, InstalledAppFlow, _build = _require_libs()

    token_path = settings.gmail_token_file
    creds: Credentials | None = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), settings.gmail_scopes)
        except (ValueError, KeyError):  # corrupt/old token → re-auth
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, settings)
            return creds
        except Exception as exc:  # noqa: BLE001 - refresh can fail many ways
            logger.warning("gmail.refresh_failed", error=str(exc))
            creds = None

    if not interactive:
        raise GmailUnavailable(
            "Gmail is not connected (no valid token). Run:  honestapply gmail-connect"
        )

    cred_file = settings.gmail_credentials_file
    if not cred_file.exists():
        raise GmailUnavailable(
            f"OAuth client secrets not found at {cred_file}. Create a Google Cloud "
            "OAuth client (Desktop app), download the JSON, and save it there. "
            "See docs/GMAIL_SETUP.md."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), settings.gmail_scopes)
    creds = flow.run_local_server(port=0)
    _save_token(creds, settings)
    return creds


def _save_token(creds: "Credentials", settings: Settings) -> None:
    token_path = settings.gmail_token_file
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)  # the token is a bearer credential — keep it private
    except OSError:  # pragma: no cover - non-POSIX
        pass


def get_service(settings: Settings | None = None, *, interactive: bool = False):
    """Build an authenticated Gmail API service client."""
    settings = settings or get_settings()
    _Request, _Credentials, _Flow, build = _require_libs()
    creds = connect(settings, interactive=interactive)
    # cache_discovery=False avoids a noisy warning and a file-cache write.
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def list_recent_ids(service, *, after_days: int, max_results: int) -> list[str]:
    """Message ids received within the last *after_days* days, newest first,
    capped at *max_results*. Uses Gmail's ``after:`` query; paginates as needed."""
    from datetime import timedelta

    after = (datetime.now(timezone.utc) - timedelta(days=max(1, after_days))).strftime("%Y/%m/%d")
    query = f"after:{after} -in:chats"
    ids: list[str] = []
    page_token: str | None = None
    while len(ids) < max_results:
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(100, max_results - len(ids)),
                pageToken=page_token,
            )
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_results]


def _header(headers: list[dict[str, str]], name: str) -> str:
    name = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


def _decode_b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):  # pragma: no cover - malformed part
        return ""


def _extract_body(payload: dict[str, Any]) -> str:
    """Prefer the text/plain part; fall back to stripped text/html, then nothing.
    Walks the MIME tree iteratively so deeply nested multiparts are handled."""
    plain: list[str] = []
    html: list[str] = []
    stack = [payload]
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime == "text/plain" and data:
            plain.append(_decode_b64url(data))
        elif mime == "text/html" and data:
            html.append(_decode_b64url(data))
        stack.extend(part.get("parts", []) or [])
    if plain:
        return "\n".join(plain).strip()
    if html:
        import re

        text = re.sub(r"<[^>]+>", " ", "\n".join(html))
        return re.sub(r"[ \t]+", " ", text).strip()
    return ""


def get_message(service, msg_id: str) -> ParsedEmail:
    """Fetch and parse one message into a :class:`ParsedEmail`."""
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    raw_from = _header(headers, "From")
    sender = parseaddr(raw_from)[1] or raw_from
    domain = sender.split("@", 1)[1].lower() if "@" in sender else ""
    received: datetime | None = None
    internal = msg.get("internalDate")
    if internal:
        try:
            received = datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc)
        except (ValueError, OSError):  # pragma: no cover
            received = None
    return ParsedEmail(
        msg_id=msg_id,
        thread_id=msg.get("threadId", ""),
        sender=sender,
        sender_domain=domain,
        subject=_header(headers, "Subject"),
        snippet=msg.get("snippet", ""),
        body=_extract_body(payload),
        received_at=received,
    )


# ---------------------------------------------------------------------------
# Write (opt-in; needs the compose scope)
# ---------------------------------------------------------------------------
def create_draft(service, *, to: str, subject: str, body: str, thread_id: str | None = None) -> str:
    """Create a Gmail draft (never sends). Returns the draft id. Requires the
    ``compose`` scope — connect with ``gmail_scope_level=compose`` first."""
    from email.mime.text import MIMEText  # stdlib

    mime = MIMEText(body)
    mime["To"] = to
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("utf-8")
    message: dict[str, Any] = {"message": {"raw": raw}}
    if thread_id:
        message["message"]["threadId"] = thread_id
    draft = service.users().drafts().create(userId="me", body=message).execute()
    return draft.get("id", "")
