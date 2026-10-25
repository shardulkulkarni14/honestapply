"""Shared HTML-to-text helper for the ATS feed adapters."""

from __future__ import annotations

import html as _html
import re

_TAG = re.compile(r"<[^>]+>")


def strip_html(markup: str | None) -> str:
    """Flatten an HTML fragment to whitespace-normalised text."""
    if not markup:
        return ""
    text = _TAG.sub(" ", markup)
    text = _html.unescape(text)
    return " ".join(text.split())
