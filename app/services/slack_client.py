"""
Slack client helpers — DMs, group DMs, message updates.
"""
from datetime import date, datetime, timedelta
from typing import Optional
from slack_sdk.web.async_client import AsyncWebClient

from app.config import settings

_client: Optional[AsyncWebClient] = None


def get_slack_client() -> AsyncWebClient:
    global _client
    if _client is None:
        _client = AsyncWebClient(token=settings.slack_bot_token)
    return _client


async def send_dm(slack_user_id: str, text: str, blocks=None) -> Optional[str]:
    """
    Open a DM with a user and post a message.
    Returns the message ts or None on failure.
    """
    client = get_slack_client()
    try:
        conv = await client.conversations_open(users=slack_user_id)
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=blocks,
        )
        return result["ts"]
    except Exception:
        return None


async def send_group_dm(user_ids: list[str], text: str, blocks=None) -> Optional[str]:
    """
    Open a group DM with multiple users and post a message.
    Returns the message ts or None on failure.
    """
    client = get_slack_client()
    try:
        conv = await client.conversations_open(users=",".join(user_ids))
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=blocks,
        )
        return result["ts"]
    except Exception:
        return None


async def update_message(channel_id: str, ts: str, text: str, blocks=None) -> None:
    client = get_slack_client()
    try:
        await client.chat_update(
            channel=channel_id,
            ts=ts,
            text=text,
            blocks=blocks,
        )
    except Exception:
        pass


async def notify_student_hours(
    student_id: int,
    mentor_slack_id: Optional[str] = None,
) -> bool:
    """
    Send a student a DM with their season total and current-week hours vs requirement.
    If behind on hours, opens a group DM with the student + all mentors who share
    the student's team and focus category.
    Returns True if the message was sent, False if the student has no Slack UID.
    """
    from sqlalchemy import func, select
    from app.database import AsyncSessionLocal
    from app.models import AttendanceSession, Mentor, Student, WeeklyRequirement

    week_start = date.today() - timedelta(days=date.today().weekday())
    week_end = week_start + timedelta(days=7)

    async with AsyncSessionLocal() as db:
        # Load student
        s_result = await db.execute(
            select(Student).where(Student.id == student_id)
        )
        student = s_result.scalars().first()
        if not student or not student.slack_user_id:
            return False

        # Season total
        season_result = await db.execute(
            select(func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0))
            .where(
                AttendanceSession.student_id == student_id,
                AttendanceSession.sign_out_time.is_not(None),
            )
        )
        season_total = float(season_result.scalar() or 0.0)

        # This week
        week_result = await db.execute(
            select(func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0))
            .where(
                AttendanceSession.student_id == student_id,
                AttendanceSession.sign_out_time.is_not(None),
                AttendanceSession.sign_in_time >= datetime.combine(week_start, datetime.min.time()),
                AttendanceSession.sign_in_time < datetime.combine(week_end, datetime.min.time()),
            )
        )
        week_hours = float(week_result.scalar() or 0.0)

        # Weekly requirement — filtered by student's category
        req_result = await db.execute(
            select(WeeklyRequirement)
            .where(
                WeeklyRequirement.team_id == student.team_id,
                WeeklyRequirement.category == student.category,
                WeeklyRequirement.week_start <= week_start,
            )
            .order_by(WeeklyRequirement.week_start.desc())
            .limit(1)
        )
        req = req_result.scalars().first()
        required = req.required_hours if req else 11.0

        # Find all mentors matching the student's team + category
        mentor_q = select(Mentor).where(
            Mentor.team_id == student.team_id,
            Mentor.category == student.category,
            Mentor.slack_user_id.is_not(None),
        )
        mentor_result = await db.execute(mentor_q)
        matched_mentors = mentor_result.scalars().all()

        # Fall back to the single supplied mentor_slack_id if no matched mentors found
        if matched_mentors:
            mentor_ids = [m.slack_user_id for m in matched_mentors]
        elif mentor_slack_id:
            mentor_ids = [mentor_slack_id]
        else:
            mentor_ids = []

    on_track = week_hours >= required
    status_icon = "✅" if on_track else "⚠️"
    week_str = week_start.strftime("%b %d")

    text = (
        f"{status_icon} *Hours Update for {student.name}*\n"
        f"Week of {week_str}: *{week_hours:.1f} / {required:.1f} hrs*\n"
        f"Season total: *{season_total:.1f} hrs*"
    )
    if on_track:
        text += "\nYou're on track — great work! 💪"
    else:
        remaining = required - week_hours
        text += f"\n_{remaining:.1f} hrs still needed this week. Get to the shop!_"

    if not on_track and mentor_ids:
        await send_group_dm([student.slack_user_id] + mentor_ids, text)
    else:
        await send_dm(student.slack_user_id, text)

    return True
