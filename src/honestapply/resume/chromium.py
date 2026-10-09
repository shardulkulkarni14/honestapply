"""Chromium "print to PDF" renderer — an OPTIONAL alternative to WeasyPrint.

WeasyPrint needs the system Pango/cairo/gobject stack (a recurring macOS/Linux
install headache). This module renders the SAME Jinja HTML to PDF via headless
Chrome instead, so a user can drop the Pango dependency by setting
``RESUME_RENDERER=chromium``. WeasyPrint stays the default.

Two backends, tried in order:

1. The ``playwright`` package's bundled Chromium, if the optional ``[chromium]``
   extra is installed (``pip install -e ".[chromium]" && playwright install
   chromium``). No system Chrome needed.
2. A detected Chrome/Chromium *binary* driven with ``--headless
   --print-to-pdf``. No new Python dependency at all — handy when the user
   already has Chrome.

ATS note: Chrome print-to-PDF of this single-column HTML preserves the DOM
reading order in the text layer (verified in tests against the bundled example
résumé), so the floated-dates extraction-order hazard does not apply here any
more than it does to WeasyPrint — the fix stays in the template.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Common Chrome/Chromium locations beyond anything on PATH. Checked after an
# explicit CHROME_BINARY / HONESTAPPLY_CHROME_BINARY env override and after PATH.
_MAC_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
_LINUX_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
]
_PATH_NAMES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
]

_RENDER_TIMEOUT_SECONDS = 120


class ChromiumNotAvailable(RuntimeError):
    """Neither playwright's Chromium nor a Chrome/Chromium binary was found."""


def _env_binary() -> str | None:
    for var in ("HONESTAPPLY_CHROME_BINARY", "CHROME_BINARY", "CHROME_PATH"):
        val = os.environ.get(var)
        if val and Path(val).exists():
            return val
    return None


def find_chrome_binary() -> str | None:
    """Path to a usable Chrome/Chromium binary, or None. Honours a
    CHROME_BINARY / HONESTAPPLY_CHROME_BINARY / CHROME_PATH env override first,
    then PATH, then the usual per-OS install locations."""
    if (override := _env_binary()) is not None:
        return override
    for name in _PATH_NAMES:
        if (found := shutil.which(name)):
            return found
    candidates = _MAC_PATHS if sys.platform == "darwin" else _LINUX_PATHS
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def _playwright_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def chromium_available() -> bool:
    """True if the chromium renderer can run here (playwright Chromium or a
    Chrome/Chromium binary is present). Tests skip cleanly when this is False."""
    return _playwright_available() or find_chrome_binary() is not None


def _with_base(html: str, base_url: str | None) -> str:
    """Inject ``<base href=...>`` so relative resources (e.g. the template's
    ``modern.css``) resolve, exactly as WeasyPrint's ``base_url`` makes them."""
    if not base_url:
        return html
    tag = f'<base href="{base_url}">'
    if "<head>" in html:
        return html.replace("<head>", "<head>" + tag, 1)
    if "<html" in html:  # no explicit <head>: put it right after the <html ...>
        head, sep, tail = html.partition(">")
        return head + sep + tag + tail
    return tag + html


def _render_with_playwright(html_file: Path, output_path: Path) -> bool:
    """Render via playwright's bundled Chromium. Returns False (without raising)
    if playwright is importable but its browser isn't installed, so the caller
    can fall back to a system binary."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.goto(html_file.as_uri(), wait_until="load")
                # prefer_css_page_size honours the template's @page size/margins.
                page.pdf(
                    path=str(output_path),
                    print_background=True,
                    prefer_css_page_size=True,
                )
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — treat a missing/broken browser as "fall back"
        from honestapply.logging_setup import get_logger

        get_logger(__name__).warning("chromium.playwright_unavailable", error=str(exc))
        return False
    return output_path.exists() and output_path.stat().st_size > 0


def _render_with_binary(html_file: Path, output_path: Path) -> None:
    binary = find_chrome_binary()
    if binary is None:
        raise ChromiumNotAvailable(
            "No Chrome/Chromium found. Install the optional 'chromium' extra "
            "(pip install -e '.[chromium]' && playwright install chromium), or a "
            "Chrome/Chromium binary, or set RESUME_RENDERER=weasyprint."
        )
    cmd = [
        binary,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",  # keep the date/URL chrome off the ATS text layer
        f"--print-to-pdf={output_path}",
        html_file.as_uri(),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_RENDER_TIMEOUT_SECONDS
    )
    if not (output_path.exists() and output_path.stat().st_size > 0):
        raise RuntimeError(
            f"Chrome print-to-pdf produced no output (exit {proc.returncode}): "
            f"{proc.stderr.strip()[-500:]}"
        )


def render_html_to_pdf(html: str, output_path: str | Path, *, base_url: str | None = None) -> Path:
    """Render *html* to a PDF at *output_path* using headless Chromium.

    Prefers playwright's bundled Chromium; falls back to a detected
    Chrome/Chromium binary. Raises ChromiumNotAvailable if neither exists.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = _with_base(html, base_url)
    # Write to a real file (not set_content) so a file:// origin can load the
    # file:// <base> resources — both backends then resolve relative URLs.
    with tempfile.TemporaryDirectory(prefix="honestapply-chromium-") as d:
        html_file = Path(d) / "document.html"
        html_file.write_text(html, encoding="utf-8")
        if not _render_with_playwright(html_file, output_path):
            _render_with_binary(html_file, output_path)
    return output_path
