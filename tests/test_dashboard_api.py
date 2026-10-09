"""The dashboard is writable: PATCH a job's status/notes, read its timeline."""

from __future__ import annotations

import pytest

from honestapply.db.models import Status


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    import dashboard.api as api

    c = TestClient(api.app)
    c.cookies.set("ha_token", api.DASH_TOKEN)  # dashboard requires the local-auth token
    return c


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

    # Append-only, spelled out: the job's *real* status column did not move, and
    # every pre-existing event is byte-for-byte identical (nothing was rewritten).
    row = next(r for r in client.get("/api/applications").json() if r["job_id"] == jid)
    assert row["status"] == Status.INTERVIEWING
    assert after[: len(before)] == before


def test_add_empty_note_is_rejected(client, add_job):
    jid = add_job(status=Status.APPLIED)
    assert client.post(f"/api/jobs/{jid}/events", json={"note": "   "}).status_code == 400


def test_add_note_to_unknown_job_is_404(client):
    assert client.post("/api/jobs/999999/events", json={"note": "x"}).status_code == 404


# --- run control / funnel / applications / activity --------------------------
def test_patch_rejects_pipeline_internal_status(client, add_job):
    """A non-tech user must not be able to re-arm the automation from the UI."""
    jid = add_job(status=Status.APPLIED)
    assert client.patch(f"/api/jobs/{jid}", json={"status": "ready_to_apply"}).status_code == 400
    assert client.patch(f"/api/jobs/{jid}", json={"status": "discovered"}).status_code == 400


def test_funnel_is_cumulative(client, add_job):
    jid = add_job(status=Status.APPLIED)
    client.patch(f"/api/jobs/{jid}", json={"status": Status.SCREENING})
    client.patch(f"/api/jobs/{jid}", json={"status": Status.REJECTED})
    f = client.get("/api/funnel").json()
    stages = {s["key"]: s["count"] for s in f["stages"]}
    # passed through applied + screening, so both count it even though now rejected
    assert stages["applied"] >= 1 and stages["in_process"] >= 1
    outcomes = {o["key"]: o["count"] for o in f["outcomes"]}
    assert outcomes["rejected"] >= 1
    assert "total" in f and "scanned" in f


def test_applications_orders_active_first(client, add_job):
    add_job(company="Zeta", status=Status.APPLIED)
    add_job(company="Alpha", status=Status.INTERVIEWING, url="https://x/2")
    statuses = [r["status"] for r in client.get("/api/applications").json()]
    assert statuses.index("interviewing") < statuses.index("applied")


def test_activity_timestamps_carry_utc_offset(client, add_job):
    jid = add_job(status=Status.APPLIED)
    client.patch(f"/api/jobs/{jid}", json={"status": Status.REJECTED})
    acts = client.get("/api/activity").json()
    assert acts and acts[0]["at"].endswith("+00:00")


def test_run_endpoints(client, monkeypatch):
    import dashboard.api as api

    class FakeProc:
        def __init__(self):
            self.stdout = iter(["starting\n", "done\n"])
            self.returncode, self.pid = 0, 999999

        def poll(self):
            return 0  # already finished, for a deterministic test

        def wait(self):
            return 0

    monkeypatch.setattr(api.subprocess, "Popen", lambda *a, **k: FakeProc())
    assert client.post("/api/run", json={"stage": "discover"}).status_code == 200
    assert client.get("/api/run").status_code == 200
    assert client.post("/api/run", json={"stage": "not-a-stage"}).status_code == 400


def test_archive_hides_job_and_unarchive_restores(client, add_job):
    """Archiving soft-deletes: the job leaves the default list but is recoverable."""
    jid = add_job(status=Status.APPLIED)
    assert any(r["job_id"] == jid for r in client.get("/api/applications").json())

    resp = client.patch(f"/api/jobs/{jid}", json={"archived": True})
    assert resp.status_code == 200 and resp.json()["archived"] is True

    # gone from the default list, still present with include_archived
    assert not any(r["job_id"] == jid for r in client.get("/api/applications").json())
    shown = client.get("/api/applications?include_archived=1").json()
    row = next(r for r in shown if r["job_id"] == jid)
    assert row["archived"] is True

    # unarchive restores it
    assert client.patch(f"/api/jobs/{jid}", json={"archived": False}).json()["archived"] is False
    assert any(r["job_id"] == jid for r in client.get("/api/applications").json())


def test_archived_job_is_excluded_from_the_funnel(client, add_job):
    jid = add_job(status=Status.APPLIED)
    before = next(s for s in client.get("/api/funnel").json()["stages"] if s["key"] == "applied")["count"]
    client.patch(f"/api/jobs/{jid}", json={"archived": True})
    after = next(s for s in client.get("/api/funnel").json()["stages"] if s["key"] == "applied")["count"]
    assert after == before - 1


def test_requires_local_auth_token():
    from fastapi.testclient import TestClient

    import dashboard.api as api

    anon = TestClient(api.app)  # no token cookie
    assert anon.get("/api/applications").status_code == 401
    anon.cookies.set("ha_token", api.DASH_TOKEN)
    assert anon.get("/api/applications").status_code == 200
