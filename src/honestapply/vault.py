"""Local secrets vault for site-account passwords (apply-stage auto-signup).

honestapply can create accounts on job portals that force one. The passwords it
generates **must never be readable by the LLM apply agent**, printed, logged, or
committed: they live only in a local, OS-backed secret store and are injected into
the browser at the DOM boundary by the Playwright-MCP proxy, never passing through
the agent's context.

Backends (auto-selected; override with ``HONESTAPPLY_VAULT_BACKEND``):

- ``keyring`` — the OS secret store (macOS Keychain / Windows Credential Manager /
  Linux Secret Service) via the ``keyring`` library. Default on a desktop.
- ``file`` — a Fernet-encrypted file under ``~/.honestapply``, with the key taken
  from ``HONESTAPPLY_VAULT_KEY`` (preferred in production) or a ``0600`` key file.
  For headless servers / CI where no OS keyring is available.

A names-only index (site + account email + timestamps, **no passwords**) is kept
alongside so ``honestapply accounts`` can list / rotate without reading a secret.

The only function that returns a plaintext password is :meth:`Vault.peek`, which
exists solely for the injection boundary. Nothing else returns, logs, or prints a
secret — the public create/rotate paths return booleans, never the value.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVICE = "honestapply-site-accounts"
_INDEX_FILE = "vault_index.json"
_KEY_FILE = "vault.key"
_ENC_FILE = "vault.enc"
_PW_PREFIX = "pw:"


class VaultError(RuntimeError):
    """The secrets vault could not be opened or used."""


# ---------------------------------------------------------------------------
# Password generation
# ---------------------------------------------------------------------------
_SYMBOLS = "!@#$%^&*-_=+"


def generate_password(length: int = 20) -> str:
    """A strong random password guaranteed to mix lower/upper/digit/symbol.

    Many portals enforce a character-class policy; token_urlsafe alone can (rarely)
    miss a class, so we regenerate until all four are present."""
    length = max(length, 12)
    pool = string.ascii_letters + string.digits + _SYMBOLS
    while True:
        pw = "".join(_secrets.choice(pool) for _ in range(length))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in _SYMBOLS for c in pw)
        ):
            return pw


def normalize_site(site: str) -> str:
    """Reduce a URL or host to a stable per-site key (registrable-ish host)."""
    s = (site or "").strip().lower()
    s = re.sub(r"^[a-z]+://", "", s)  # drop scheme
    s = s.split("/")[0].split("?")[0]  # host only
    s = s.split("@")[-1]  # drop any userinfo
    return s.strip(". ")


def _pw_key(site: str) -> str:
    return _PW_PREFIX + normalize_site(site)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class _KeyringBackend:
    def __init__(self) -> None:
        try:
            import keyring  # noqa: PLC0415 — optional dep, imported lazily
        except Exception as exc:  # pragma: no cover
            raise VaultError(
                "The `keyring` package is required for the OS secret store. "
                "Install it with: pip install -e '.[accounts]'."
            ) from exc
        self._kr = keyring

    def get(self, key: str) -> str | None:
        return self._kr.get_password(SERVICE, key)

    def set(self, key: str, value: str) -> None:
        self._kr.set_password(SERVICE, key, value)

    def delete(self, key: str) -> None:
        try:
            self._kr.delete_password(SERVICE, key)
        except Exception:  # noqa: BLE001 — deleting a missing key is a no-op
            pass


class _FileBackend:
    """Fernet-encrypted {key: secret} map on disk (0600)."""

    def __init__(self, path: Path, key: bytes) -> None:
        try:
            from cryptography.fernet import Fernet  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover
            raise VaultError(
                "The `cryptography` package is required for the encrypted-file "
                "vault. Install it with: pip install -e '.[accounts]'."
            ) from exc
        self._path = path
        self._f = Fernet(key)

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._f.decrypt(self._path.read_bytes()).decode())
        except Exception as exc:  # noqa: BLE001
            raise VaultError(f"Could not decrypt the vault at {self._path}: {exc}") from exc

    def _save(self, data: dict[str, str]) -> None:
        self._path.write_bytes(self._f.encrypt(json.dumps(data).encode()))
        os.chmod(self._path, 0o600)

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        d = self._load()
        d[key] = value
        self._save(d)

    def delete(self, key: str) -> None:
        d = self._load()
        if d.pop(key, None) is not None:
            self._save(d)


def _resolve_fernet_key(config_dir: Path) -> bytes:
    """Prefer an env-supplied key (production); else a local 0600 key file."""
    env_key = os.environ.get("HONESTAPPLY_VAULT_KEY", "").strip()
    if env_key:
        return env_key.encode()
    key_path = config_dir / _KEY_FILE
    if key_path.exists():
        return key_path.read_bytes()
    from cryptography.fernet import Fernet  # noqa: PLC0415

    key = Fernet.generate_key()
    key_path.write_bytes(key)
    os.chmod(key_path, 0o600)
    return key


def _keyring_usable() -> bool:
    try:
        import keyring  # noqa: PLC0415
        from keyring.backends.fail import Keyring as FailKeyring  # noqa: PLC0415
    except Exception:
        return False
    try:
        return not isinstance(keyring.get_keyring(), FailKeyring)
    except Exception:  # pragma: no cover
        return False


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------
class Vault:
    """A secret store plus a names-only index. Never prints or returns a secret
    except via :meth:`peek` (used only by the injection boundary)."""

    def __init__(self, backend: Any, config_dir: Path) -> None:
        self._b = backend
        self._config_dir = config_dir
        self._index_path = config_dir / _INDEX_FILE

    # -- index (metadata only — NO passwords) --------------------------------
    def _read_index(self) -> dict[str, dict[str, Any]]:
        if not self._index_path.exists():
            return {}
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _write_index(self, idx: dict[str, dict[str, Any]]) -> None:
        self._index_path.write_text(json.dumps(idx, indent=2), encoding="utf-8")
        os.chmod(self._index_path, 0o600)

    # -- public API ----------------------------------------------------------
    def has_password(self, site: str) -> bool:
        return self._b.get(_pw_key(site)) is not None

    def ensure_password(self, site: str, email: str = "", *, length: int = 20) -> bool:
        """Create+store a strong password for *site* if none exists.

        Returns True if a new password was created, False if one already existed.
        Never returns the password itself."""
        site_n = normalize_site(site)
        idx = self._read_index()
        now = datetime.now(timezone.utc).isoformat()
        if self.has_password(site_n):
            entry = idx.setdefault(site_n, {"created_at": now})
            entry["last_used_at"] = now
            if email:
                entry.setdefault("email", email)
            self._write_index(idx)
            return False
        self._b.set(_pw_key(site_n), generate_password(length))
        idx[site_n] = {"email": email, "created_at": now, "last_used_at": now}
        self._write_index(idx)
        return True

    def peek(self, site: str) -> str | None:
        """Return the plaintext password — INTERNAL, for the injection boundary
        only. Never log, print, or return this to the agent."""
        return self._b.get(_pw_key(site))

    def rotate(self, site: str, *, length: int = 20) -> bool:
        """Replace the stored password with a fresh one. Returns True if a site
        entry existed to rotate."""
        if not self.has_password(site):
            return False
        self._b.set(_pw_key(site), generate_password(length))
        idx = self._read_index()
        if normalize_site(site) in idx:
            idx[normalize_site(site)]["last_used_at"] = datetime.now(timezone.utc).isoformat()
            self._write_index(idx)
        return True

    def delete(self, site: str) -> bool:
        existed = self.has_password(site)
        self._b.delete(_pw_key(site))
        idx = self._read_index()
        if idx.pop(normalize_site(site), None) is not None:
            self._write_index(idx)
        return existed

    def list_sites(self) -> list[dict[str, Any]]:
        """Site metadata only (site, email, timestamps) — never passwords."""
        return [{"site": k, **v} for k, v in sorted(self._read_index().items())]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_vault(
    *,
    backend: str | None = None,
    config_dir: Path | str | None = None,
) -> Vault:
    """Open the vault with the configured/auto-detected backend."""
    cfg = Path(os.path.expanduser(str(config_dir or "~/.honestapply")))
    cfg.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(cfg, 0o700)
    except OSError:  # pragma: no cover
        pass

    choice = (backend or os.environ.get("HONESTAPPLY_VAULT_BACKEND", "")).strip().lower()
    if not choice:
        choice = "keyring" if _keyring_usable() else "file"

    if choice == "keyring":
        return Vault(_KeyringBackend(), cfg)
    if choice == "file":
        return Vault(_FileBackend(cfg / _ENC_FILE, _resolve_fernet_key(cfg)), cfg)
    raise VaultError(f"Unknown vault backend: {choice!r} (expected 'keyring' or 'file').")
