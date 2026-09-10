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


def test_events_interleaves_application_rows(client, add_job):
    """The timeline shows submission attempts (applications) alongside status changes."""
    from honestapply.db.models import Application
    from honestapply.db.session import session_scope

    jid = add_job(status=Status.APPLIED)
    with session_scope() as s:
        s.add(
            Application(
                job_id=jid, mode="real", status="applied",
                confirmation_text="Your application was received.",
            )
        )

    items = client.get(f"/api/jobs/{jid}/events").json()
    kinds = {it["kind"] for it in items}
    assert "status" in kinds and "application" in kinds
    app = next(it for it in items if it["kind"] == "application")
    assert app["mode"] == "real"
    assert app["id"]  # every item is identified


def test_add_note_appends_to_history_append_only(client, add_job):
    """POST /events adds a same-status marker; it never edits or drops history."""
    jid = add_job(status=Status.INTERVIEWING)
    before = client.get(f"/api/jobs/{jid}/events").json()

    resp = client.post(f"/api/jobs/{jid}/events", json={"note": "round 2 with the CTO"})
    assert resp.status_code == 200

    after = client.get(f"/api/jobs/{jid}/events").json()
    assert len(after) == len(before) + 1
    added = after[-1]
    assert added["kind"] == "status"
    # same-status marker => status is unchanged, note is preserved, dashboard-sourced
    assert added["from"] == Status.INTERVIEWING
    assert added["to"] == Status.INTERVIEWING
    assert added["source"] == "dashboard"
    assert added["note"] == "round 2 with the CTO"


def test_add_empty_note_is_rejected(client, add_job):
    jid = add_job(status=Status.APPLIED)
    assert client.post(f"/api/jobs/{jid}/events", json={"note": "   "}).status_code == 400


def test_add_note_to_unknown_job_is_404(client):
    assert client.post("/api/jobs/999999/events", json={"note": "x"}).status_code == 404
