"""Tests for the Report screen's name-click session-history modal
(`/admin/report/students/{id}/sessions`) and the `badge_url` Jinja global that powers
the QR-badge links on Roster and in that same modal."""
from datetime import datetime, timedelta

import pytest

from app.models import AttendanceSession, SessionStatus
from app.services.badge import compute_badge_id, effective_code
from app.services.sso import SSO_COOKIE
from app.utils import utc_to_local
from tests.conftest import make_sso_cookie

pytestmark = pytest.mark.asyncio


async def _add_session(db, student_id, *, hours_ago, status=SessionStatus.contributor, sign_out=True):
    sign_in = datetime.utcnow() - timedelta(hours=hours_ago)
    db.add(AttendanceSession(
        student_id=student_id,
        sign_in_time=sign_in,
        sign_out_time=sign_in + timedelta(hours=1) if sign_out else None,
        status=status,
        hours_counted=1.0 if sign_out else None,
    ))
    await db.commit()


async def test_sessions_fragment_orders_most_recent_first(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_session(db, ada.id, hours_ago=48)
    await _add_session(db, ada.id, hours_ago=2)
    await _add_session(db, ada.id, hours_ago=24)

    resp = await authed_client.get(f"/admin/report/students/{ada.id}/sessions")
    assert resp.status_code == 200

    # The 2-hours-ago session's sign-in time should appear before the 24- and
    # 48-hour-ago ones in the rendered HTML (most recent on top).
    #
    # Dates are stored naive-UTC but rendered through `utc_to_local`, so the expected
    # strings must be converted too. Deriving them straight from `utcnow()` matched only
    # while the UTC and local dates happened to agree, and broke every day between UTC
    # midnight and local midnight — including on CI, since `utc_to_local` follows the
    # app's `timezone` setting rather than the machine's.
    html = resp.text
    pos_2h = html.find(utc_to_local(datetime.utcnow() - timedelta(hours=2)).strftime("%m/%d"))
    pos_48h = html.find(utc_to_local(datetime.utcnow() - timedelta(hours=48)).strftime("%m/%d"))
    assert pos_2h != -1 and pos_48h != -1
    assert pos_2h < pos_48h


async def test_sessions_fragment_shows_status_badge_and_open_session(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_session(db, ada.id, hours_ago=5, status=SessionStatus.contributor)
    await _add_session(db, ada.id, hours_ago=1, sign_out=False)

    resp = await authed_client.get(f"/admin/report/students/{ada.id}/sessions")
    assert resp.status_code == 200
    assert "Contributor" in resp.text
    assert "Open" in resp.text


async def test_sessions_fragment_empty_state(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    resp = await authed_client.get(f"/admin/report/students/{ada.id}/sessions")
    assert resp.status_code == 200
    assert "No sessions yet." in resp.text


async def test_sessions_fragment_404_for_unknown_student(authed_client):
    resp = await authed_client.get("/admin/report/students/999999/sessions")
    assert resp.status_code == 404
    assert "Student not found" in resp.text


async def test_sessions_fragment_requires_auth(client, make_student, db):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    resp = await client.get(f"/admin/report/students/{ada.id}/sessions", follow_redirects=False)
    assert resp.status_code == 303


async def test_manager_can_reach_sessions_fragment(client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))
    resp = await client.get(f"/admin/report/students/{ada.id}/sessions")
    assert resp.status_code == 200


# ── badge_url() Jinja global ────────────────────────────────────────────────────

async def test_badge_url_matches_compute_badge_id(db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")

    from app.routers.admin import _badge_url

    expected = f"/badge/{compute_badge_id(effective_code(ada))}"
    assert _badge_url(ada) == expected


async def test_roster_page_renders_qr_badge_link(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    expected_href = f"/badge/{compute_badge_id(effective_code(ada))}"

    resp = await authed_client.get("/admin/roster")
    assert resp.status_code == 200
    assert expected_href in resp.text


async def test_report_page_renders_badge_url_data_attribute(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    expected_href = f"/badge/{compute_badge_id(effective_code(ada))}"

    resp = await authed_client.get("/admin/report")
    assert resp.status_code == 200
    assert f'data-badge-url="{expected_href}"' in resp.text
