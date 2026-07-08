"""Tests for the /qr Slack slash command — DMs the caller their own kiosk QR badge,
looked up by their Slack user id, for both students and mentors."""
import app.routers.slack as slack_router
from app.models import Mentor


async def _no_signature_check(request):
    return b""


def _bypass_signature(monkeypatch):
    monkeypatch.setattr(slack_router, "_verify_slack_signature", _no_signature_check)


def _stub_send_qr_dm(monkeypatch, result=True):
    calls = []

    async def _fake(slack_user_id, code, name):
        calls.append((slack_user_id, code, name))
        return result

    monkeypatch.setattr(slack_router, "send_qr_dm", _fake)
    return calls


async def _add_mentor(db, slack_id, name="Coach Ray", code="mnt00001"):
    m = Mentor(name=name, slack_user_id=slack_id, member_code=code, is_active=True)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def test_qr_sends_badge_to_student(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    calls = _stub_send_qr_dm(monkeypatch)
    student = await make_student(name="Ada Lovelace", code="ada00001")
    student.slack_user_id = "USTU"
    await db.commit()

    resp = await client.post("/slack/command", data={"command": "/qr", "user_id": "USTU"})

    assert resp.status_code == 200
    assert "Sent your QR badge" in resp.text
    assert calls == [("USTU", "ada00001", "Ada Lovelace")]


async def test_qr_sends_badge_to_mentor(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    calls = _stub_send_qr_dm(monkeypatch)
    await _add_mentor(db, "UMENTOR", name="Coach Ray", code="mnt00001")

    resp = await client.post("/slack/command", data={"command": "/qr", "user_id": "UMENTOR"})

    assert resp.status_code == 200
    assert "Sent your QR badge" in resp.text
    assert calls == [("UMENTOR", "mnt00001", "Coach Ray")]


async def test_qr_unknown_slack_user_gets_error(client, monkeypatch):
    _bypass_signature(monkeypatch)
    calls = _stub_send_qr_dm(monkeypatch)

    resp = await client.post("/slack/command", data={"command": "/qr", "user_id": "UNOBODY"})

    assert resp.status_code == 200
    assert "isn't linked to a student or mentor record" in resp.text
    assert calls == []


async def test_qr_reports_failure_when_dm_fails(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    _stub_send_qr_dm(monkeypatch, result=False)
    student = await make_student(name="Ada Lovelace", code="ada00001")
    student.slack_user_id = "USTU"
    await db.commit()

    resp = await client.post("/slack/command", data={"command": "/qr", "user_id": "USTU"})

    assert resp.status_code == 200
    assert "Couldn't send your QR badge" in resp.text


async def test_qr_inactive_student_falls_through_to_mentor_check(client, db, make_student, monkeypatch):
    """An inactive student with the same slack id shouldn't be matched — falls through
    (and, finding no active mentor either, gets the standard error)."""
    _bypass_signature(monkeypatch)
    calls = _stub_send_qr_dm(monkeypatch)
    student = await make_student(name="Graduated Grace", code="grad0001", is_active=False)
    student.slack_user_id = "UGRAD"
    await db.commit()

    resp = await client.post("/slack/command", data={"command": "/qr", "user_id": "UGRAD"})

    assert resp.status_code == 200
    assert "isn't linked to a student or mentor record" in resp.text
    assert calls == []
