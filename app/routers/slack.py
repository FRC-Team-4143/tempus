"""
Slack routes — slash commands and interactive component handler.

Slack sends:
  POST /slack/command   — slash commands (verified by signing secret)
  POST /slack/interact  — interactive button actions (verified by signing secret)
"""
import hashlib
import hmac
import json
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import AttendanceSession, Mentor, SessionStatus, Student
from app.services.attendance import sign_out, get_signed_in_students
from app.services.broadcaster import broadcaster
from app.services.slack_client import get_slack_client, update_message

router = APIRouter(prefix="/slack")


# ── Signature verification ─────────────────────────────────────────────────────

async def _verify_slack_signature(request: Request) -> bytes:
    """Read raw body and verify Slack request signature. Raises 403 on failure."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    # Reject requests older than 5 minutes (replay protection)
    try:
        if abs(time.time() - float(timestamp)) > 300:
            raise HTTPException(status_code=403, detail="Request too old")
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid timestamp")

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = (
        "v0="
        + hmac.new(
            settings.slack_signing_secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid Slack signature")
    return body


def _checkout_blocks(
    session: AttendanceSession,
    student: Student,
) -> list[dict]:
    """Build the interactive message blocks for a checkout request."""
    elapsed = datetime.utcnow() - session.sign_in_time
    hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
    minutes = remainder // 60
    sign_in_str = session.sign_in_time.strftime("%I:%M %p")

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Checkout Request — {student.name}*\n"
                    f"Team {student.team.number} · Signed in at {sign_in_str} "
                    f"({hours}h {minutes:02d}m ago)"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"checkout_{session.id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Contributor (full hours)"},
                    "style": "primary",
                    "action_id": "checkout_contributor",
                    "value": str(session.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔸 Present (50% hours)"},
                    "action_id": "checkout_present",
                    "value": str(session.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Distraction (0% hours)"},
                    "style": "danger",
                    "action_id": "checkout_distraction",
                    "value": str(session.id),
                },
            ],
        },
    ]


# ── /checkout slash command ────────────────────────────────────────────────────

async def _send_hours_dm(slack_user_id: str) -> None:
    """Send a student their own hours summary — no mentor included."""
    from app.database import AsyncSessionLocal
    from app.models import Student, AttendanceSession, WeeklyRequirement
    from sqlalchemy import func
    from datetime import date, timedelta

    week_start = date.today() - timedelta(days=date.today().weekday())
    week_end = week_start + timedelta(days=7)

    async with AsyncSessionLocal() as db:
        s_result = await db.execute(
            select(Student).where(Student.slack_user_id == slack_user_id, Student.active.is_(True))
        )
        student = s_result.scalars().first()
        if not student:
            from app.services.slack_client import send_dm
            await send_dm(slack_user_id, "❌ Your Slack account isn't linked to a student record. Please ask a mentor.")
            return

        season_result = await db.execute(
            select(func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0))
            .where(AttendanceSession.student_id == student.id, AttendanceSession.sign_out_time.is_not(None))
        )
        season_total = float(season_result.scalar() or 0.0)

        week_result = await db.execute(
            select(func.coalesce(func.sum(AttendanceSession.hours_counted), 0.0))
            .where(
                AttendanceSession.student_id == student.id,
                AttendanceSession.sign_out_time.is_not(None),
                AttendanceSession.sign_in_time >= datetime.combine(week_start, datetime.min.time()),
                AttendanceSession.sign_in_time < datetime.combine(week_end, datetime.min.time()),
            )
        )
        week_hours = float(week_result.scalar() or 0.0)

        req_result = await db.execute(
            select(WeeklyRequirement)
            .where(
                WeeklyRequirement.team_id == student.team_id,
                WeeklyRequirement.week_start <= week_start,
            )
            .order_by(WeeklyRequirement.week_start.desc())
            .limit(1)
        )
        req = req_result.scalars().first()
        required = req.required_hours if req else 11.0

    on_track = week_hours >= required
    status_icon = "✅" if on_track else "⚠️"
    week_str = week_start.strftime("%b %d")

    text = (
        f"{status_icon} *Your Hours — Week of {week_str}*\n"
        f"This week: *{week_hours:.1f} / {required:.1f} hrs*\n"
        f"Season total: *{season_total:.1f} hrs*"
    )
    if on_track:
        text += "\nYou're on track — great work! 💪"
    else:
        remaining = required - week_hours
        text += f"\n_{remaining:.1f} hrs still needed this week. Get to the shop!_"

    from app.services.slack_client import send_dm
    await send_dm(slack_user_id, text)


async def _send_checkout_dm(session_id: int, user_id: str) -> None:
    """Send the checkout interactive message to the mentor as a background task."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AttendanceSession)
            .options(selectinload(AttendanceSession.student).selectinload(Student.team))
            .where(AttendanceSession.id == session_id)
        )
        session = result.scalars().first()
        if not session:
            return

        client = get_slack_client()
        blocks = _checkout_blocks(session, session.student)
        msg = await client.chat_postMessage(
            channel=user_id,
            text=f"Checkout request for {session.student.name}",
            blocks=blocks,
        )
        session.slack_message_ts = msg["ts"]
        session.slack_channel_id = msg["channel"]
        await db.commit()


@router.post("/command")
async def slack_command(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    await _verify_slack_signature(request)

    form = await request.form()
    command = form.get("command", "")
    text = (form.get("text") or "").strip()
    channel_id = form.get("channel_id", "")
    user_id = form.get("user_id", "")

    if command == "/hours":
        background_tasks.add_task(_send_hours_dm, user_id)
        return Response(content="Fetching your hours... check your DMs.", media_type="text/plain")

    if command != "/checkout":
        return Response(content="Unknown command.", media_type="text/plain")

    if not text:
        return Response(
            content="Usage: `/checkout <badge_id or student name>`",
            media_type="text/plain",
        )

    # Verify the caller is a known mentor
    mentor_result = await db.execute(
        select(Mentor).where(Mentor.slack_user_id == user_id)
    )
    mentor = mentor_result.scalars().first()
    if not mentor:
        return Response(
            content="❌ Only registered mentors can check out students.",
            media_type="text/plain",
        )

    # Find open session by partial or exact name match
    open_sessions = await get_signed_in_students(db)

    lower = text.lower()
    matches = [s for s in open_sessions if lower in s.student.name.lower()]

    if len(matches) == 0:
        return Response(
            content=f"No signed-in student found matching '{text}'.",
            media_type="text/plain",
        )
    elif len(matches) == 1:
        session = matches[0]
    else:
        # Multiple partial matches — try for an exact name match first
        exact = [s for s in matches if s.student.name.lower() == lower]
        if len(exact) == 1:
            session = exact[0]
        else:
            names = ", ".join(s.student.name for s in matches)
            return Response(
                content=f"Multiple students match '{text}': {names}. Please be more specific.",
                media_type="text/plain",
            )

    student = session.student

    # Respond to Slack immediately (must be within 3 seconds)
    # The DM is sent asynchronously so we don't block the response
    background_tasks.add_task(_send_checkout_dm, session.id, user_id)

    return Response(
        content=f"Sending checkout request for {student.name}... check your DMs.",
        media_type="text/plain",
    )


# ── Interactive actions handler ────────────────────────────────────────────────

@router.post("/interact")
async def slack_interact(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _verify_slack_signature(request)

    form = await request.form()
    payload_str = form.get("payload", "")
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid payload")

    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    session_id_str = action.get("value", "")
    channel_id = payload.get("channel", {}).get("id") or (
        payload.get("container", {}).get("channel_id", "")
    )
    message_ts = payload.get("message", {}).get("ts", "")

    if action_id not in ("checkout_contributor", "checkout_present", "checkout_distraction"):
        return Response(status_code=200)

    try:
        session_id = int(session_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session id")

    status = (
        SessionStatus.contributor
        if action_id == "checkout_contributor"
        else SessionStatus.present
        if action_id == "checkout_present"
        else SessionStatus.distraction
    )

    session = await sign_out(db, session_id, status)
    if not session:
        # Already signed out or not found
        await update_message(
            channel_id,
            message_ts,
            "⚠️ This session has already been closed.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "⚠️ This checkout request has already been processed.",
                    },
                }
            ],
        )
        return Response(status_code=200)

    await broadcaster.broadcast("update")

    student = session.student
    out_time = session.sign_out_time.strftime("%I:%M %p")
    hours = session.hours_counted
    status_label = (
        "✅ Contributor" if status == SessionStatus.contributor
        else "🔸 Present" if status == SessionStatus.present
        else "🚫 Distraction"
    )

    await update_message(
        channel_id or session.slack_channel_id,
        message_ts or session.slack_message_ts,
        f"Checked out {student.name}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{student.name}* signed out at {out_time}\n"
                        f"Status: {status_label} · *{hours:.2f} hrs* recorded"
                    ),
                },
            }
        ],
    )

    return Response(status_code=200)
