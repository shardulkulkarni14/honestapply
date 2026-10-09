"""Tests for the notification dispatcher (Telegram / ntfy). No network: requests
is monkeypatched. Verifies payloads, is_configured, and the disabled no-op."""

from __future__ import annotations

from honestapply import notify as N
from honestapply.config import Settings


class FakeResp:
    def __init__(self, ok: bool = True):
        self.ok = ok


def test_telegram_sends_with_url(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, **kw):
        captured.update(url=url, json=json)
        return FakeResp(True)

    monkeypatch.setattr(N.requests, "post", fake_post)
    s = Settings(honestapply_notify_provider="telegram", telegram_bot_token="T:1", telegram_chat_id="99")
    assert N.is_configured(s) is True
    assert N.notify("Title", "msg", url="https://live.example", settings=s) is True
    assert "api.telegram.org/botT:1/sendMessage" in captured["url"]
    assert captured["json"]["chat_id"] == "99"
    assert "https://live.example" in captured["json"]["text"]


def test_ntfy_sends_with_action(monkeypatch):
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        captured.update(url=url, headers=headers, data=data)
        return FakeResp(True)

    monkeypatch.setattr(N.requests, "post", fake_post)
    s = Settings(honestapply_notify_provider="ntfy", ntfy_topic="abc123", ntfy_url="https://ntfy.sh")
    assert N.is_configured(s) is True
    assert N.notify("T", "m", url="https://live.example", settings=s) is True
    assert captured["url"].endswith("/abc123")
    assert "https://live.example" in captured["headers"]["Actions"]


def test_disabled_is_noop(monkeypatch):
    called = []
    monkeypatch.setattr(N.requests, "post", lambda *a, **k: called.append(1) or FakeResp())
    s = Settings(honestapply_notify_provider="none")
    assert N.is_configured(s) is False
    assert N.notify("T", "m", settings=s) is False
    assert called == []


def test_telegram_unconfigured_is_false(monkeypatch):
    monkeypatch.setattr(N.requests, "post", lambda *a, **k: FakeResp())
    s = Settings(honestapply_notify_provider="telegram")  # no token / chat id
    assert N.is_configured(s) is False
    assert N.notify("T", "m", settings=s) is False


def test_notify_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(N.requests, "post", boom)
    s = Settings(honestapply_notify_provider="telegram", telegram_bot_token="T:1", telegram_chat_id="99")
    assert N.notify("T", "m", settings=s) is False  # swallowed, not raised
