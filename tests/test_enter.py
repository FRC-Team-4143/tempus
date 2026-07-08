"""Tests for the one-tap /enter bridge — the target of the /hours "open my dashboard"
link. Mirrors Munus's /enter, but passes an absolute return_to so a fresh (cookie-less)
sign-in lands back on Tempus's own host after Legion approves."""
import app.routers.portal as portal
from app.models import Mentor
from app.services.sso import SSO_COOKIE
from tests.conftest import make_sso_cookie


def _stub_start_challenge(monkeypatch, result="https://legion.example.org/sso/pending/nonce123"):
    calls = []

    async def _fake(member_code, *, return_to="/me"):
        calls.append((member_code, return_to))
        return result

    monkeypatch.setattr(portal.legion_auth, "start_challenge", _fake)
    return calls


async def _add_mentor(db, *, code="mnt00001", name="Coach Ray", is_active=True):
    m = Mentor(name=name, member_code=code, slack_user_id=f"U{code}", is_active=is_active)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def test_signed_in_short_circuits_to_next(client, monkeypatch):
    calls = _stub_start_challenge(monkeypatch)
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=[], member_code="ada00001", role="student"))

    resp = await client.get("/enter?member=ada00001&next=/me", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/me"
    assert calls == []  # no Legion round trip when already signed in


async def test_unknown_member_falls_back_to_authorize(client, monkeypatch):
    calls = _stub_start_challenge(monkeypatch)
    resp = await client.get("/enter?member=doesnotexist", follow_redirects=False)

    assert resp.status_code == 303
    assert "/sso/authorize?app=tempus" in resp.headers["location"]
    assert calls == []


async def test_blank_member_falls_back_to_authorize(client, monkeypatch):
    _stub_start_challenge(monkeypatch)
    resp = await client.get("/enter", follow_redirects=False)
    assert resp.status_code == 303
    assert "/sso/authorize?app=tempus" in resp.headers["location"]


async def test_known_student_starts_challenge_with_absolute_return_to(client, db, make_student, monkeypatch):
    calls = _stub_start_challenge(monkeypatch)
    await make_student(code="ada00001")

    resp = await client.get("/enter?member=ada00001&next=/me", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://legion.example.org/sso/pending/nonce123"
    assert calls == [("ada00001", "http://localhost:8000/me")]


async def test_known_mentor_starts_challenge(client, db, monkeypatch):
    calls = _stub_start_challenge(monkeypatch)
    await _add_mentor(db, code="mnt00001")

    resp = await client.get("/enter?member=mnt00001&next=/me", follow_redirects=False)

    assert resp.status_code == 303
    assert calls == [("mnt00001", "http://localhost:8000/me")]


async def test_inactive_member_falls_back_to_authorize(client, db, make_student, monkeypatch):
    calls = _stub_start_challenge(monkeypatch)
    await make_student(code="grad0001", is_active=False)

    resp = await client.get("/enter?member=grad0001", follow_redirects=False)

    assert resp.status_code == 303
    assert "/sso/authorize?app=tempus" in resp.headers["location"]
    assert calls == []


async def test_legion_unavailable_returns_503(client, db, make_student, monkeypatch):
    _stub_start_challenge(monkeypatch, result=None)
    await make_student(code="ada00001")

    resp = await client.get("/enter?member=ada00001", follow_redirects=False)

    assert resp.status_code == 503
    assert "temporarily unavailable" in resp.text


async def test_bad_next_is_sanitized_to_me(client, db, make_student, monkeypatch):
    """An open-redirect attempt in `next` is coerced to /me by safe_next before it can
    reach start_challenge's return_to."""
    calls = _stub_start_challenge(monkeypatch)
    await make_student(code="ada00001")

    resp = await client.get("/enter?member=ada00001&next=//evil.com", follow_redirects=False)

    assert resp.status_code == 303
    assert calls == [("ada00001", "http://localhost:8000/me")]
