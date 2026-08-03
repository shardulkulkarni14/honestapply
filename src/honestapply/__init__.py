"""honestapply — autonomous job-application pipeline."""

__version__ = "0.1.0"


def _ensure_native_lib_path() -> None:
    """macOS: WeasyPrint loads Pango/cairo/gobject via dlopen, which doesn't
    search Homebrew's lib dir by default. Prepend the *arch-matching* Homebrew
    lib dir to DYLD_FALLBACK_LIBRARY_PATH before any weasyprint import, so PDF
    rendering works with no manual env setup.

    Critically, we pick by CPU arch: an arm64 process must NOT search
    /usr/local/lib first if it holds x86_64 dylibs (Intel Homebrew leftovers) —
    dlopen would hit the wrong-arch lib and hard-fail instead of falling through.
    (dlopen consults this path per-call, so setting it here — pre-import — works.)"""
    import os
    import platform
    import sys

    if sys.platform != "darwin":
        return
    # arm64 -> /opt/homebrew, x86_64 -> /usr/local
    brew_lib = "/opt/homebrew/lib" if platform.machine() == "arm64" else "/usr/local/lib"
    if not os.path.isdir(brew_lib):
        return
    existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    parts = [p for p in existing.split(":") if p and p != brew_lib]
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join([brew_lib, *parts])


_ensure_native_lib_path()
