"""Tests for the reports service: the student_ids scoping filter, the mentor weekly-
hours analogue, and the shared default-date-range helper used by /admin/report,
/admin/report/export, and the personal portal's own report table."""
from datetime import date, datetime, timedelta

from app.models import AttendanceSession, Mentor, MentorSession, WeeklyRequirement
from app.services.reports import (
    default_report_range, drop_zero_requirement_weeks, week_starts_in_range, weekly_attendance_report,
    weekly_mentor_hours,
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


async def test_archived_student_excluded_by_default(db, make_student):
    """An archived (graduated/alumni) student must never appear in the default
    report browse — only students, mentors, and admins should see who's on the
    active roster."""
    await make_student(name="Ada Lovelace", code="ada00001")
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_session(db, grad.id, hours=5.0)

    today = date.today()
    week_starts = week_starts_in_range(today, today)
    rows = await weekly_attendance_report(db, week_starts)

    assert [r["student"].name for r in rows] == ["Ada Lovelace"]


async def test_archived_student_reachable_via_explicit_student_ids(db, make_student):
    """A deliberate by-id lookup (the admin archived-member search) must still work
    for an archived student even though they're excluded from the default browse."""
    grad = await make_student(name="Old Grad", code="grad0001", is_active=False)
    await _add_session(db, grad.id, hours=5.0)

    today = date.today()
    week_starts = week_starts_in_range(today, today)
    rows = await weekly_attendance_report(db, week_starts, student_ids=[grad.id])

    assert len(rows) == 1
    assert rows[0]["student"].id == grad.id
    assert rows[0]["total_hours"] == 5.0


# ── weeks_met / weeks_total exclude 0-requirement weeks ────────────────────────

async def test_weeks_total_excludes_zero_requirement_weeks(db, make_student, team):
    ada = await make_student(name="Ada Lovelace", code="ada00001")

    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(weeks=1)

    # last_monday is a scheduled 0-hour off week; this_monday resumes at 11.0. A
    # requirement holds until overridden by a newer entry, so both rows are needed —
    # otherwise the 0.0 would also apply going forward to this_monday.
    db.add(WeeklyRequirement(team_id=team.id, subteam_slug=None, week_start=last_monday, required_hours=0.0))
    db.add(WeeklyRequirement(team_id=team.id, subteam_slug=None, week_start=this_monday, required_hours=11.0))
    await db.commit()

    week_starts = week_starts_in_range(last_monday, this_monday)
    rows = await weekly_attendance_report(db, week_starts, team_id=team.id)

    assert len(rows) == 1
    assert rows[0]["weeks_total"] == 1
    assert rows[0]["weeks_met"] == 0  # no hours logged this week, so the one counted week is unmet


# ── drop_zero_requirement_weeks ─────────────────────────────────────────────────

def test_drop_zero_requirement_weeks_drops_only_all_zero_columns():
    w1, w2, w3 = date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)
    rows = [
        {
            "weeks": [
                {"week_start": w1, "hours": 0.0, "required": 0.0, "met": True},
                {"week_start": w2, "hours": 3.0, "required": 11.0, "met": False},
                {"week_start": w3, "hours": 0.0, "required": 0.0, "met": True},
            ],
        },
        {
            "weeks": [
                {"week_start": w1, "hours": 0.0, "required": 0.0, "met": True},
                {"week_start": w2, "hours": 12.0, "required": 11.0, "met": True},
                # w3 stays even though this row is 0 too, since another row is nonzero at w2 only —
                # w3 is genuinely all-zero across every row and should drop.
                {"week_start": w3, "hours": 0.0, "required": 0.0, "met": True},
            ],
        },
    ]

    new_week_starts, new_rows = drop_zero_requirement_weeks([w1, w2, w3], rows)

    assert new_week_starts == [w2]
    assert [w["week_start"] for w in new_rows[0]["weeks"]] == [w2]
    assert [w["week_start"] for w in new_rows[1]["weeks"]] == [w2]


def test_drop_zero_requirement_weeks_keeps_column_if_any_row_nonzero():
    w1, w2 = date(2026, 6, 1), date(2026, 6, 8)
    rows = [
        {"weeks": [{"week_start": w1, "hours": 0.0, "required": 0.0, "met": True},
                    {"week_start": w2, "hours": 0.0, "required": 0.0, "met": True}]},
        {"weeks": [{"week_start": w1, "hours": 5.0, "required": 6.0, "met": False},
                    {"week_start": w2, "hours": 0.0, "required": 0.0, "met": True}]},
    ]

    new_week_starts, _ = drop_zero_requirement_weeks([w1, w2], rows)

    assert new_week_starts == [w1]  # w1 kept (row 2 has a nonzero requirement); w2 dropped


def test_drop_zero_requirement_weeks_noop_for_mentor_rows():
    w1 = date(2026, 6, 1)
    rows = [{"weeks": [{"week_start": w1, "hours": 2.0}]}]

    new_week_starts, new_rows = drop_zero_requirement_weeks([w1], rows)

    assert new_week_starts == [w1]
    assert new_rows == rows


def test_drop_zero_requirement_weeks_empty_rows_passthrough():
    assert drop_zero_requirement_weeks([], []) == ([], [])


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
