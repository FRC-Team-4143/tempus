"""Tests for the CSV exports added for outside programs (e.g. Silver Cords, which wants
a list of everyone at 200+ hours):

  * /admin/report/export?mode=totals — a flat one-row-per-student totals file over an
    arbitrary range, with no 26-week cap.
  * /admin/report/archived/{students,mentors}/{id}/export — one member's raw session
    history, honoring the detail page's own date range.
"""
import csv
import io
from datetime import datetime, timedelta

import pytest

from app.models import AttendanceSession, Mentor, MentorSession

pytestmark = pytest.mark.asyncio


async def _add_session(db, student_id, *, days_ago, hours=3.0):
    sign_in = datetime.utcnow() - timedelta(days=days_ago, hours=hours)
    db.add(AttendanceSession(
        student_id=student_id,
        sign_in_time=sign_in,
        sign_out_time=sign_in + timedelta(hours=hours),
        hours_counted=hours,
    ))
    await db.commit()


def _rows(resp):
    """Parse a CSV response into (header, list-of-rows)."""
    parsed = list(csv.reader(io.StringIO(resp.text)))
    return parsed[0], parsed[1:]


def _row_for(rows, name):
    return next(r for r in rows if r[0] == name)


# ── Totals export ──────────────────────────────────────────────────────────────

async def test_totals_export_one_row_per_student(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    grace = await make_student(name="Grace Hopper", code="gh000001")
    await _add_session(db, ada.id, days_ago=3, hours=4.0)
    await _add_session(db, ada.id, days_ago=2, hours=2.5)
    await _add_session(db, grace.id, days_ago=1, hours=1.0)

    resp = await authed_client.get("/admin/report/export?mode=totals")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]

    header, rows = _rows(resp)
    assert header == [
        "Name", "Member Code", "Team", "Subteam", "Status",
        "Total Hours", "Sessions", "Range Start", "Range End",
    ]
    assert len(rows) == 2
    assert _row_for(rows, "Ada Lovelace")[5] == "6.50"
    assert _row_for(rows, "Ada Lovelace")[6] == "2"
    assert _row_for(rows, "Grace Hopper")[5] == "1.00"


async def test_totals_export_includes_students_with_no_hours(authed_client, make_student):
    """A roster file needs the zero rows too — a missing line reads as "not on the
    team", not "no hours"."""
    await make_student(name="Ada Lovelace", code="ada00001")

    resp = await authed_client.get("/admin/report/export?mode=totals")
    _, rows = _rows(resp)
    assert _row_for(rows, "Ada Lovelace")[5] == "0.00"


async def test_totals_export_honors_date_range(authed_client, db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_session(db, ada.id, days_ago=400, hours=9.0)   # outside
    await _add_session(db, ada.id, days_ago=5, hours=2.0)     # inside

    d_from = (datetime.utcnow() - timedelta(days=10)).date().isoformat()
    d_to = (datetime.utcnow() + timedelta(days=1)).date().isoformat()
    resp = await authed_client.get(
        f"/admin/report/export?mode=totals&date_from={d_from}&date_to={d_to}"
    )
    _, rows = _rows(resp)
    row = _row_for(rows, "Ada Lovelace")
    assert row[5] == "2.00"
    assert row[7] == d_from
    assert row[8] == d_to


async def test_totals_export_spans_more_than_26_weeks(authed_client, db, make_student):
    """The whole reason totals mode exists: the weekly grid's `week_starts_in_range`
    caps at 26 weeks, so a multi-season request silently loses everything older. Totals
    mode has no cap."""
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_session(db, ada.id, days_ago=700, hours=120.0)
    await _add_session(db, ada.id, days_ago=2, hours=80.0)

    d_from = (datetime.utcnow() - timedelta(days=1000)).date().isoformat()
    d_to = (datetime.utcnow() + timedelta(days=1)).date().isoformat()

    totals = await authed_client.get(
        f"/admin/report/export?mode=totals&date_from={d_from}&date_to={d_to}"
    )
    _, rows = _rows(totals)
    assert _row_for(rows, "Ada Lovelace")[5] == "200.00"

    # The same range through the weekly grid is worse than merely truncated:
    # `week_starts_in_range` keeps the *first* 26 Mondays from date_from, so a
    # multi-year ask returns a window from three years ago and reports 0.00 for a
    # student who has 200 hours. That's what totals mode exists to avoid.
    grid = await authed_client.get(
        f"/admin/report/export?date_from={d_from}&date_to={d_to}"
    )
    grid_header, grid_rows = _rows(grid)
    assert len(grid_header) == 3 + 26 + 2  # 3 leading + the 26-week cap + 2 trailing
    assert _row_for(grid_rows, "Ada Lovelace")[-2] == "0.00"


async def test_totals_export_archived_flag(authed_client, db, make_student):
    """A four-year cords window reaches students who have since been archived, so
    they're opt-in rather than absent."""
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_session(db, grad.id, days_ago=500, hours=210.0)

    default = await authed_client.get("/admin/report/export?mode=totals")
    _, rows = _rows(default)
    assert not any(r[0] == "Old Grad" for r in rows)

    with_archived = await authed_client.get("/admin/report/export?mode=totals&archived=1")
    _, rows = _rows(with_archived)
    row = _row_for(rows, "Old Grad")
    assert row[4] == "archived"
    assert row[5] == "210.00"


async def test_totals_export_filters_by_team_and_subteam(authed_client, db, make_student, team):
    ada = await make_student(name="Ada Lovelace", code="ada00001", subteam_slug="software")
    await make_student(name="Bob Mech", code="bob00001", subteam_slug="mechanical")
    await _add_session(db, ada.id, days_ago=1, hours=3.0)

    resp = await authed_client.get(
        f"/admin/report/export?mode=totals&team_id={team.id}&category=software"
    )
    _, rows = _rows(resp)
    assert [r[0] for r in rows] == ["Ada Lovelace"]
    assert rows[0][3] == "software"


async def test_default_export_is_still_the_weekly_grid(authed_client, make_student):
    """Totals mode is additive — the existing button and its filename must not shift."""
    await make_student(name="Ada Lovelace", code="ada00001")

    resp = await authed_client.get("/admin/report/export")
    assert resp.status_code == 200
    header, _ = _rows(resp)
    assert header[:3] == ["Student", "Team", "Subteam"]
    assert "weekly_report_" in resp.headers["content-disposition"]


async def test_totals_export_filename_reflects_range(authed_client, make_student):
    await make_student(name="Ada Lovelace", code="ada00001")

    all_time = await authed_client.get("/admin/report/export?mode=totals")
    assert "tempus_hour_totals_all-time.csv" in all_time.headers["content-disposition"]

    ranged = await authed_client.get(
        "/admin/report/export?mode=totals&date_from=2022-08-01&date_to=2026-06-01"
    )
    assert "tempus_hour_totals_2022-08-01_2026-06-01.csv" in ranged.headers["content-disposition"]


# ── Per-member detail export ───────────────────────────────────────────────────

async def test_student_session_export_rows_and_range(authed_client, db, make_student):
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_session(db, grad.id, days_ago=400, hours=3.0)
    await _add_session(db, grad.id, days_ago=2, hours=2.0)

    all_time = await authed_client.get(f"/admin/report/archived/students/{grad.id}/export")
    assert all_time.status_code == 200
    assert "text/csv" in all_time.headers["content-type"]
    header, rows = _rows(all_time)
    assert header == [
        "Name", "Member Code", "Team", "Sign In", "Sign Out", "Status", "Hours Counted",
    ]
    assert len(rows) == 2
    assert {r[6] for r in rows} == {"3.00", "2.00"}
    assert rows[0][1] == "grad0001"
    assert "old-grad_sessions_all-time.csv" in all_time.headers["content-disposition"]

    d_from = (datetime.utcnow() - timedelta(days=10)).date().isoformat()
    d_to = (datetime.utcnow() + timedelta(days=1)).date().isoformat()
    ranged = await authed_client.get(
        f"/admin/report/archived/students/{grad.id}/export?date_from={d_from}&date_to={d_to}"
    )
    _, rows = _rows(ranged)
    assert [r[6] for r in rows] == ["2.00"]


async def test_mentor_session_export(authed_client, db):
    mentor = Mentor(name="Coach Ray", slack_user_id="Uray", member_code="ray00001")
    db.add(mentor)
    await db.commit()
    await db.refresh(mentor)
    sign_in = datetime.utcnow() - timedelta(days=1, hours=2)
    db.add(MentorSession(
        mentor_id=mentor.id,
        sign_in_time=sign_in,
        sign_out_time=sign_in + timedelta(hours=2),
        hours_counted=2.0,
    ))
    await db.commit()

    resp = await authed_client.get(f"/admin/report/archived/mentors/{mentor.id}/export")
    assert resp.status_code == 200
    header, rows = _rows(resp)
    # No Status column — MentorSession has none.
    assert header == ["Name", "Member Code", "Team", "Sign In", "Sign Out", "Hours Counted"]
    assert rows[0][0] == "Coach Ray"
    assert rows[0][5] == "2.00"


async def test_member_export_missing_member_redirects_to_search(authed_client):
    resp = await authed_client.get(
        "/admin/report/archived/students/99999/export", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/report/search"


async def test_manager_can_reach_the_new_exports(client, make_student):
    """`tempus-manager` is scoped to /admin/report/* — the new routes live there, so a
    manager who can already read the report can pull the same data as a file."""
    from app.services.sso import SSO_COOKIE
    from tests.conftest import make_sso_cookie

    student = await make_student(name="Ada Lovelace", code="ada00001")
    client.cookies.set(SSO_COOKIE, make_sso_cookie(groups=["tempus-manager"]))

    totals = await client.get("/admin/report/export?mode=totals", follow_redirects=False)
    assert totals.status_code == 200

    detail = await client.get(
        f"/admin/report/archived/students/{student.id}/export", follow_redirects=False
    )
    assert detail.status_code == 200
