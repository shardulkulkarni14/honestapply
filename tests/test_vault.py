"""Vault tests — hermetic: force the encrypted-file backend into a temp dir with
a generated key, so nothing touches the real OS keyring or ~/.honestapply.

Verifies: password generation policy, create-once/idempotence, peek returns the
stored secret, rotate changes it, delete removes it, the index holds metadata but
never the password, and site normalization.
"""

from __future__ import annotations

import json

import pytest

from honestapply import vault as V


@pytest.fixture
def file_vault(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("HONESTAPPLY_VAULT_BACKEND", "file")
    monkeypatch.setenv("HONESTAPPLY_VAULT_KEY", Fernet.generate_key().decode())
    return V.get_vault(config_dir=tmp_path), tmp_path


def test_generate_password_policy():
    for _ in range(50):
        pw = V.generate_password(16)
        assert len(pw) == 16
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(c in V._SYMBOLS for c in pw)


def test_normalize_site():
    assert V.normalize_site("https://careers.acme.com/jobs/123?x=1") == "careers.acme.com"
    assert V.normalize_site("ACME.com/") == "acme.com"


def test_ensure_is_create_once_and_peek_roundtrips(file_vault):
    v, _ = file_vault
    assert v.has_password("careers.acme.com") is False
    assert v.ensure_password("https://careers.acme.com/jobs/1", "me@example.com") is True
    assert v.has_password("careers.acme.com") is True
    pw = v.peek("careers.acme.com")
    assert pw and len(pw) >= 12
    # second call does not create / change it
    assert v.ensure_password("careers.acme.com") is False
    assert v.peek("careers.acme.com") == pw


def test_rotate_changes_secret(file_vault):
    v, _ = file_vault
    v.ensure_password("portal.example", "me@example.com")
    before = v.peek("portal.example")
    assert v.rotate("portal.example") is True
    assert v.peek("portal.example") != before
    assert v.rotate("never-seen.example") is False


def test_delete_removes_secret_and_index(file_vault):
    v, _ = file_vault
    v.ensure_password("gone.example", "me@example.com")
    assert v.delete("gone.example") is True
    assert v.has_password("gone.example") is False
    assert v.peek("gone.example") is None
    assert v.list_sites() == []
    assert v.delete("gone.example") is False  # already gone


def test_index_has_metadata_but_never_the_password(file_vault):
    v, tmp = file_vault
    v.ensure_password("secretsite.example", "me@example.com")
    pw = v.peek("secretsite.example")
    index_text = (tmp / "vault_index.json").read_text()
    idx = json.loads(index_text)
    assert "secretsite.example" in idx
    assert idx["secretsite.example"]["email"] == "me@example.com"
    assert "created_at" in idx["secretsite.example"]
    # the plaintext password must NOT appear anywhere in the index
    assert pw not in index_text
    # and the encrypted blob must not contain it in cleartext
    enc = (tmp / "vault.enc").read_bytes()
    assert pw.encode() not in enc


def test_listing_is_metadata_only(file_vault):
    v, _ = file_vault
    v.ensure_password("a.example", "a@example.com")
    v.ensure_password("b.example", "b@example.com")
    sites = v.list_sites()
    assert {s["site"] for s in sites} == {"a.example", "b.example"}
    # no secret-bearing key in the listed metadata
    assert all("password" not in s and "secret" not in s for s in sites)
