"""Ping the human when honestapply needs them (CAPTCHA, login wall, …).

A thin dispatcher over Telegram (default) and ntfy. The apply stage uses it to
send your phone a message with a live-view URL, so you can solve a CAPTCHA
remotely and let the SAME run continue. It is a no-op (returns False) when nothing
is configured, so callers fall back to needs_human.
"""

from __future__ import annotations

import requests

from honestapply.config import get_settings
from honestapply.logging_setup import get_logger

log = get_logger(__name__)


def is_configured(settings=None) -> bool:
    settings = settings or get_settings()
    provider = (settings.honestapply_notify_provider or "none").lower()
    if provider == "telegram":
        return bool(settings.telegram_bot_token and settings.telegram_chat_id)
    if provider == "ntfy":
        return bool(settings.ntfy_topic)
    return False


def notify(
    title: str,
    message: str,
    *,
    url: str | None = None,
    priority: str = "high",
    settings=None,
) -> bool:
    """Send a notification. Returns True on success, False if not configured or on
    error (never raises — a failed ping must not abort an apply run)."""
    settings = settings or get_settings()
    provider = (settings.honestapply_notify_provider or "none").lower()
    try:
        if provider == "telegram":
            ok = _telegram(settings, title, message, url)
        elif provider == "ntfy":
            ok = _ntfy(settings, title, message, url, priority)
        else:
            return False
    except Exception as exc:  # noqa: BLE001 — notifications are best-effort
        log.warning("notify.failed", provider=provider, error=str(exc))
        return False
    if ok:
        log.info("notify.sent", provider=provider, title=title)
    return ok


def _telegram(settings, title: str, message: str, url: str | None) -> bool:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return False
    text = f"*{title}*\n{message}"
    if url:
        text += f"\n\n[Open & solve →]({url})"
    resp = requests.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
        json={
            "chat_id": settings.telegram_chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    return resp.ok


def _ntfy(settings, title: str, message: str, url: str | None, priority: str) -> bool:
    if not settings.ntfy_topic:
        return False
    headers = {"Title": title, "Priority": priority, "Tags": "warning"}
    body = message
    if url:
        # ntfy action button + the raw link in the body as a fallback
        headers["Actions"] = f"view, Open & solve, {url}"
        body = f"{message}\n{url}"
    resp = requests.post(
        f"{settings.ntfy_url.rstrip('/')}/{settings.ntfy_topic}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    return resp.ok
