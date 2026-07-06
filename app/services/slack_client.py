"""
Slack client helpers — DMs, group DMs, message updates.
"""
from datetime import datetime, timedelta
from typing import Optional
from slack_sdk.web.async_client import AsyncWebClient

from app.config import settings
from app.utils import today_local, local_to_utc

_client: Optional[AsyncWebClient] = None


def get_slack_client() -> AsyncWebClient:
    global _client
    if _client is None:
        _client = AsyncWebClient(token=settings.slack_bot_token)
    return _client


async def send_dm(slack_user_id: str, text: str, blocks=None, automated: bool = False) -> Optional[str]:
    """
    Open a DM with a user and post a message.
    Returns the message ts or None on failure.
    If automated=True, skips sending when updates_enabled is false.
    """
    if automated and not settings.updates_enabled:
        return None
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


async def send_group_dm(user_ids: list[str], text: str, blocks=None, automated: bool = False) -> Optional[str]:
    """
    Open a group DM with multiple users and post a message.
    Returns the message ts or None on failure.
    If automated=True, skips sending when updates_enabled is false.
    """
    if automated and not settings.updates_enabled:
        return None
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


async def send_qr_dm(slack_user_id: str, code: str, name: str) -> bool:
    """Generate a QR code PNG for `code` and send it as a file DM to the user."""
    import io as _io
    import logging
    import qrcode
    log = logging.getLogger(__name__)

    img = qrcode.make(code)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    client = get_slack_client()
    try:
        conv = await client.conversations_open(users=slack_user_id)
        channel_id = conv["channel"]["id"]
        await client.files_upload_v2(
            channel=channel_id,
            content=buf.read(),
            filename=f"{name.replace(' ', '_')}_qr.png",
            title=f"QR Badge — {name}",
            initial_comment=(
                f"Hi {name.split()[0]}! Here's your QR badge for the shop kiosk. "
                "Screenshot or save this and scan it to sign in and out."
            ),
        )
        return True
    except Exception as e:
        log.error("send_qr_dm failed for %s (%s): %s", name, slack_user_id, e)
        return False


async def send_channel_image(
    channel_id: str,
    image_bytes: bytes,
    filename: str,
    comment: str = "",
) -> bool:
    """Upload an image to a channel with an optional lead-in comment.

    The bot must have files:write + chat:write and be a member of the channel.
    Returns True on success, False on failure.
    """
    import logging
    log = logging.getLogger(__name__)

    client = get_slack_client()
    try:
        await client.files_upload_v2(
            channel=channel_id,
            content=image_bytes,
            filename=filename,
            title=filename,
            initial_comment=comment,
        )
        return True
    except Exception as e:
        log.error("send_channel_image failed for channel %s: %s", channel_id, e)
        return False


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
    from sqlalchemy import and_, func, select
    from app.database import AsyncSessionLocal
    from app.models import AttendanceSession, Mentor, Student, Team

    week_start = today_local() - timedelta(days=today_local().weekday())
    week_end = week_start + timedelta(days=7)
    week_start_utc = local_to_utc(datetime.combine(week_start, datetime.min.time()))
    week_end_utc = local_to_utc(datetime.combine(week_end, datetime.min.time()))

    async with AsyncSessionLocal() as db:
        # Load student
        s_result = await db.execute(
            select(Student).where(Student.id == student_id)
        )
        student = s_result.scalars().first()
        if not student or not student.slack_user_id:
            return False

        # Season total — counted from the configured leaderboard cutoff (None = all-time)
        from app.services.app_settings import leaderboard_since_utc
        since_utc = await leaderboard_since_utc(db)
        season_q = (
            select(func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0))
            .where(
                AttendanceSession.student_id == student_id,
                AttendanceSession.sign_out_time.is_not(None),
            )
        )
        if since_utc is not None:
            season_q = season_q.where(AttendanceSession.sign_in_time >= since_utc)
        season_result = await db.execute(season_q)
        season_total = float(season_result.scalar() or 0.0)

        # Leaderboard rank — same metric/filters as season_total, across the two
        # program teams (4143/4423). Outer join keeps zero-hour students ranked.
        rank_join = and_(
            AttendanceSession.student_id == Student.id,
            AttendanceSession.sign_out_time.is_not(None),
        )
        if since_utc is not None:
            rank_join = and_(rank_join, AttendanceSession.sign_in_time >= since_utc)
        rank_rows = (await db.execute(
            select(
                Student.id.label("sid"),
                Student.team_id,
                Team.number.label("team_number"),
                func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0).label("total"),
            )
            .join(Team, Team.id == Student.team_id)
            .join(AttendanceSession, rank_join, isouter=True)
            .where(Team.number.in_([4143, 4423]))
            .group_by(Student.id)
        )).all()

        # Competition ranking: 1 + (students with a strictly higher total), so
        # ties share a rank. my_total matches a student's own row (same filters).
        my_total = season_total
        overall_count = len(rank_rows)
        overall_rank = 1 + sum(1 for r in rank_rows if r.total > my_total)
        team_rows = [r for r in rank_rows if r.team_id == student.team_id]
        team_count = len(team_rows)
        team_rank = 1 + sum(1 for r in team_rows if r.total > my_total)
        team_number = next((r.team_number for r in rank_rows if r.sid == student_id), None)

        # This week
        week_result = await db.execute(
            select(func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0))
            .where(
                AttendanceSession.student_id == student_id,
                AttendanceSession.sign_out_time.is_not(None),
                AttendanceSession.sign_in_time >= week_start_utc,
                AttendanceSession.sign_in_time < week_end_utc,
            )
        )
        week_hours = float(week_result.scalar() or 0.0)

        # Weekly requirement — most-specific scope first (team+category, … , all-teams)
        from app.services.requirements import resolve_requirement
        required = await resolve_requirement(db, student.team_id, student.category, week_start)

        # Find all lead mentors matching the student's team + category
        mentor_q = select(Mentor).where(
            Mentor.team_id == student.team_id,
            Mentor.category == student.category,
            Mentor.slack_user_id.is_not(None),
            Mentor.is_lead.is_(True),
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
        f"Season total: *{season_total:.1f} hrs*\n"
        f"Rank: *#{overall_rank} of {overall_count}* overall · "
        f"*#{team_rank} of {team_count}* on Team {team_number}"
    )
    if on_track:
        text += "\nYou're on track — great work! 💪"
    else:
        remaining = required - week_hours
        text += f"\n_{remaining:.1f} hrs still needed — you may need to make up hours in the upcoming week._"

    if not on_track and mentor_ids:
        await send_group_dm([student.slack_user_id] + mentor_ids, text)
    else:
        await send_dm(student.slack_user_id, text)

    return True
