"""
APScheduler jobs:
  1. Daily auto sign-out at configured time
  2. Weekly Slack DM to each student with their hours vs. requirement
"""
import logging
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import AsyncSessionLocal
from app.models import AttendanceSession, Mentor, SessionStatus, Student, FocusCategory
from app.services.attendance import sign_out_all_open, mentor_sign_out_all_open
from app.services.slack_client import send_dm, send_group_dm
from app.utils import today_local, local_to_utc

log = logging.getLogger(__name__)


def _current_week_start() -> date:
    """Return the Monday of the current week in local (CST) time."""
    today = today_local()
    return today - timedelta(days=today.weekday())


async def _get_requirement(team_id: int, week_start: date, category=None) -> float:
    """
    Return required_hours for a team+category's week, most-specific scope first
    (team+category, team, all-teams+category, all-teams), then the 11h default.
    """
    from app.services.requirements import resolve_requirement
    async with AsyncSessionLocal() as db:
        return await resolve_requirement(db, team_id, category, week_start)


async def _weekly_hours_for_student(db, student_id: int, week_start: date) -> float:
    week_end = week_start + timedelta(days=7)
    week_start_utc = local_to_utc(datetime.combine(week_start, datetime.min.time()))
    week_end_utc = local_to_utc(datetime.combine(week_end, datetime.min.time()))
    result = await db.execute(
        select(func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0))
        .where(
            AttendanceSession.student_id == student_id,
            AttendanceSession.sign_out_time.is_not(None),
            AttendanceSession.sign_in_time >= week_start_utc,
            AttendanceSession.sign_in_time < week_end_utc,
        )
    )
    return float(result.scalar() or 0.0)


async def job_auto_signout() -> None:
    log.info("Running auto sign-out job")
    async with AsyncSessionLocal() as db:
        count = await sign_out_all_open(db, status=SessionStatus.auto)
        mentor_count = await mentor_sign_out_all_open(db)
    if count:
        from app.services.broadcaster import broadcaster
        await broadcaster.broadcast("update")
    if mentor_count:
        from app.services.broadcaster import broadcaster
        await broadcaster.broadcast("mentor_update")
    log.info("Auto sign-out: closed %d student session(s), %d mentor session(s)", count, mentor_count)


async def job_weekly_dms() -> None:
    log.info("Running weekly DM job")
    week_start = _current_week_start()

    async with AsyncSessionLocal() as db:
        students_result = await db.execute(
            select(Student)
            .options(selectinload(Student.team))
            .where(
                Student.slack_user_id.is_not(None),
                Student.is_active.is_(True),
            )
        )
        students = students_result.scalars().all()

    for student in students:
        async with AsyncSessionLocal() as db:
            hours = await _weekly_hours_for_student(db, student.id, week_start)

            required = await _get_requirement(student.team_id, week_start, student.category)
            on_track = hours >= required

            # Find all mentors matching student's team + category
            mentor_result = await db.execute(
                select(Mentor).where(
                    Mentor.team_id == student.team_id,
                    Mentor.category == student.category,
                    Mentor.slack_user_id.is_not(None),
                )
            )
            matched_mentors = mentor_result.scalars().all()
            mentor_ids = [m.slack_user_id for m in matched_mentors]

        status_icon = "✅" if on_track else "⚠️"
        week_str = week_start.strftime("%b %d")

        text = (
            f"{status_icon} *Week of {week_str} — Hours Update*\n"
            f"Team {student.team.number} · {student.name}\n"
            f"*{hours:.1f} / {required:.1f} hrs* required this week"
        )
        if on_track:
            text += "\nYou're on track — great work! 💪"
        else:
            remaining = required - hours
            text += f"\n_{remaining:.1f} hrs still needed — you may need to make up hours in the upcoming week._"

        if not on_track and mentor_ids:
            await send_group_dm([student.slack_user_id] + mentor_ids, text)
        else:
            await send_dm(student.slack_user_id, text)


async def job_nightly_backup() -> None:
    from app.services.backup import is_sqlite, nightly_backup
    if not is_sqlite():
        return
    try:
        nightly_backup()
    except Exception:  # never let a backup failure crash the scheduler
        log.exception("Nightly backup failed")


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    # Auto sign-out job
    h, m = settings.auto_signout_time.split(":")
    scheduler.add_job(
        job_auto_signout,
        CronTrigger(hour=int(h), minute=int(m), timezone=settings.timezone),
        id="auto_signout",
        replace_existing=True,
    )

    # Weekly DM job
    dh, dm_ = settings.weekly_dm_time.split(":")
    scheduler.add_job(
        job_weekly_dms,
        CronTrigger(
            day_of_week=settings.weekly_dm_day,
            hour=int(dh),
            minute=int(dm_),
            timezone=settings.timezone,
        ),
        id="weekly_dms",
        replace_existing=True,
    )

    # Nightly database backup
    bh, bm = settings.backup_time.split(":")
    scheduler.add_job(
        job_nightly_backup,
        CronTrigger(hour=int(bh), minute=int(bm), timezone=settings.timezone),
        id="nightly_backup",
        replace_existing=True,
    )

    return scheduler
