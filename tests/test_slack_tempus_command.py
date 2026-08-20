"""Tests for the /tempus Slack slash command — a bare one-tap magic link to the
caller's own /me dashboard, no stats. Mirrors Munus's /munus and Legion's /legion."""
import re

import app.routers.slack as slack_router
from app.config import settings
from app.models import Mentor
from itsdangerous import URLSafeTimedSerializer


async def _no_signature_check(request):
    return b""


def _bypass_signature(monkeypatch):
    monkeypatch.setattr(slack_router, "_verify_slack_signature", _no_signature_check)


async def _add_mentor(db, slack_id, name="Coach Ray", code="mnt00001"):
    m = Mentor(name=name, slack_user_id=slack_id, member_code=code, is_active=True)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


def _link_payload(text: str) -> dict:
    signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso-link")
    (token,) = re.findall(r"/sso/link\?token=([^|>\s]+)", text)
    return signer.loads(token)


async def test_tempus_command_links_a_student_to_me(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    student = await make_student(name="Ada Lovelace", code="ada00001")
    student.slack_user_id = "USTU"
    await db.commit()

    resp = await client.post("/slack/command", data={"command": "/tempus", "user_id": "USTU"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "ephemeral"
    payload = _link_payload(body["text"])
    assert payload["member_code"] == "ada00001"
    assert payload["return_to"].endswith("/me")


async def test_tempus_command_links_a_mentor_too(client, db, monkeypatch):
    """Unlike Munus's /munus, Tempus's /me is open to mentors too — no read-only
    fallback path needed."""
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")

    resp = await client.post("/slack/command", data={"command": "/tempus", "user_id": "UMENTOR"})

    payload = _link_payload(resp.json()["text"])
    assert payload["member_code"] == "mnt00001"
    assert payload["return_to"].endswith("/me")


async def test_tempus_command_tells_an_unlinked_user_why(client, monkeypatch):
    _bypass_signature(monkeypatch)

    resp = await client.post("/slack/command", data={"command": "/tempus", "user_id": "UNOBODY"})

    assert resp.status_code == 200
    assert "isn't linked" in resp.text


async def test_tempus_command_ignores_an_archived_student(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    student = await make_student(name="Gone Already", code="gone0001")
    student.slack_user_id = "UGONE"
    student.is_active = False
    await db.commit()

    resp = await client.post("/slack/command", data={"command": "/tempus", "user_id": "UGONE"})

    assert "isn't linked" in resp.text
