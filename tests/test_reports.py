"""Tests for the reports service: the student_ids scoping filter, the mentor weekly-
hours analogue, and the shared default-date-range helper used by /admin/report,
/admin/report/export, and the personal portal's own report table."""
from datetime import date, datetime, timedelta

from app.models import AttendanceSession, Mentor, MentorSession
from app.services.reports import (
    default_report_range, week_starts_in_range, weekly_attendance_report, weekly_mentor_hours,
)


async def _add_session(db, student_id, hours, days_ago=0):
    now = datetime.utcnow() - timedelta(days=days_ago)
    db.add(AttendanceSession(
        student_id=student_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


async def _add_mentor(db, name="Coach Ray"):
    m = Mentor(name=name, slack_user_id=f"U{name}", is_active=True)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def _add_mentor_session(db, mentor_id, hours, days_ago=0):
    now = datetime.utcnow() - timedelta(days=days_ago)
    db.add(MentorSession(
        mentor_id=mentor_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


# ── weekly_attendance_report: student_ids scoping ──────────────────────────────

async def test_student_ids_scopes_to_just_that_student(db, make_student):
    ada = await make_student(name="Ada Lovelace", code="ada00001")
    grace = await make_student(name="Grace Hopper", code="grace001")
    await _add_session(db, ada.id, hours=3.0)
    await _add_session(db, grace.id, hours=4.0)

    today = date.today()
    week_starts = week_starts_in_range(today, today)

    rows = await weekly_attendance_report(db, week_starts, student_ids=[ada.id])

    assert len(rows) == 1
    assert rows[0]["student"].id == ada.id
    assert rows[0]["total_hours"] == 3.0


async def test_student_ids_none_returns_everyone(db, make_student):
    await make_student(name="Ada Lovelace", code="ada00001")
    await make_student(name="Grace Hopper", code="grace001")

    today = date.today()
    week_starts = week_starts_in_range(today, today)
    rows = await weekly_attendance_report(db, week_starts)

    assert len(rows) == 2


# ── weekly_mentor_hours ─────────────────────────────────────────────────────────

async def test_weekly_mentor_hours_buckets_by_week(db):
    mentor = await _add_mentor(db)
    await _add_mentor_session(db, mentor.id, hours=2.0, days_ago=0)

    # Comfortably mid-week *last* week (this Monday minus 5 days), regardless of
    # what weekday "today" happens to be. A fixed day-count like 7 or 8 sits right
    # on the query window's boundary when today is a Monday and flakes.
    today = date.today()
    await _add_mentor_session(db, mentor.id, hours=1.5, days_ago=today.weekday() + 5)

    week_starts = week_starts_in_range(today - timedelta(weeks=1), today)
    result = await weekly_mentor_hours(db, week_starts, mentor.id)

    assert result["mentor"].id == mentor.id
    assert result["total_hours"] == 3.5
    assert len(result["weeks"]) == 2


async def test_weekly_mentor_hours_unknown_mentor_returns_none(db):
    result = await weekly_mentor_hours(db, week_starts_in_range(date.today(), date.today()), mentor_id=999999)
    assert result is None


async def test_weekly_mentor_hours_empty_week_starts(db):
    mentor = await _add_mentor(db)
    result = await weekly_mentor_hours(db, [], mentor.id)
    assert result == {"mentor": mentor, "weeks": [], "total_hours": 0.0}


# ── default_report_range ─────────────────────────────────────────────────────────

def test_default_report_range_falls_back_to_rolling_4_weeks_when_unset():
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    start, end = default_report_range(None)
    assert end == this_monday
    assert start == this_monday - timedelta(weeks=3)


def test_default_report_range_starts_at_leaderboard_since_week():
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    since = today - timedelta(days=10)  # some Tuesday, 10 days back
    since_monday = since - timedelta(days=since.weekday())

    start, end = default_report_range(since)

    assert start == since_monday
    assert end == this_monday
