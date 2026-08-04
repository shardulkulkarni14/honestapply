"""The dashboard is writable: PATCH a job's status/notes, read its timeline."""

from __future__ import annotations

import pytest

from honestapply.db.models import Status


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import dashboard.api as api

    return TestClient(api.app)


def test_patch_sets_status_and_records_a_dashboard_event(client, add_job):
    jid = add_job(status=Status.APPLIED)

    resp = client.patch(
        f"/api/jobs/{jid}",
        json={"status": Status.INTERVIEWING, "event_note": "phone screen booked"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == Status.INTERVIEWING

    events = client.get(f"/api/jobs/{jid}/events").json()
    last = events[-1]
    assert last["to"] == Status.INTERVIEWING
    assert last["source"] == "dashboard"
    assert last["note"] == "phone screen booked"


def test_patch_updates_the_running_note(client, add_job):
    jid = add_job(status=Status.APPLIED)
    resp = client.patch(f"/api/jobs/{jid}", json={"notes": "prefers mornings"})
    assert resp.status_code == 200
    assert resp.json()["notes"] == "prefers mornings"


def test_patch_rejects_an_unknown_status(client, add_job):
    jid = add_job(status=Status.APPLIED)
    resp = client.patch(f"/api/jobs/{jid}", json={"status": "promoted-to-ceo"})
    assert resp.status_code == 400


def test_patch_unknown_job_is_404(client):
    assert client.patch("/api/jobs/999999", json={"status": Status.OFFER}).status_code == 404


def test_events_endpoint_returns_the_full_timeline(client, add_job):
    jid = add_job(status=Status.APPLIED)
    client.patch(f"/api/jobs/{jid}", json={"status": Status.SCREENING})
    client.patch(f"/api/jobs/{jid}", json={"status": Status.INTERVIEWING})

    tos = [e["to"] for e in client.get(f"/api/jobs/{jid}/events").json()]
    assert tos == [Status.APPLIED, Status.SCREENING, Status.INTERVIEWING]
