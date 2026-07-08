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
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import AttendanceSession, Mentor, MentorSession, SessionStatus, Student, Team
from app.services import audit
from app.services.attendance import update_session_status, get_signed_in_students
from app.services.broadcaster import broadcaster
from app.services.requirements import resolve_requirement
from app.services.slack_client import send_dm, send_qr_dm
from app.utils import utc_to_local, today_local, format_elapsed, current_week_bounds

router = APIRouter(prefix="/slack")


# ── Signature verification ─────────────────────────────────────────────────────

async def _verify_slack_signature(request: Request) -> bytes:
    """Read raw body and verify Slack request signature. Raises 403 on failure."""
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="Slack integration is not configured (no signing secret set).")

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


# ── /edit interactive blocks ───────────────────────────────────────────────────

_STATUS_LABELS = {
    SessionStatus.contributor: "Contributor",
    SessionStatus.present: "Present",
    SessionStatus.auto: "Auto",
    SessionStatus.distraction: "Distraction",
}


def _edit_session_list_blocks(student: Student, sessions: list) -> list[dict]:
    """Step 1 — list of the student's last 5 sessions, each as a selectable button."""
    buttons = []
    for s in sessions:
        date_str = utc_to_local(s.sign_in_time).strftime("%b %d")
        status_label = _STATUS_LABELS.get(s.status, "—") if s.status else "—"
        label = f"{date_str} · {format_elapsed(s.sign_in_time, s.sign_out_time)} · {status_label}"
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": label},
            "action_id": f"edit_select_{s.id}",
            "value": str(s.id),
        })

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Edit Session — {student.name}*\nSelect a session to change its contribution level:",
            },
        },
        {
            "type": "actions",
            "block_id": f"edit_list_{student.id}",
            "elements": buttons,
        },
    ]


def _edit_status_blocks(session: AttendanceSession, student: Student) -> list[dict]:
    """Step 2 — show the chosen session details and offer 3 status buttons."""
    date_str = utc_to_local(session.sign_in_time).strftime("%b %d")
    current = _STATUS_LABELS.get(session.status, "—") if session.status else "—"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Edit Session — {student.name}*\n"
                    f"{date_str} · {format_elapsed(session.sign_in_time, session.sign_out_time)} · Current: *{current}*\n"
                    f"Choose a new contribution level:"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"edit_status_{session.id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Contributor (full hours)"},
                    "style": "primary",
                    "action_id": "edit_contributor",
                    "value": str(session.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔸 Present (50% hours)"},
                    "action_id": "edit_present",
                    "value": str(session.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Distraction (0% hours)"},
                    "style": "danger",
                    "action_id": "edit_distraction",
                    "value": str(session.id),
                },
            ],
        },
    ]


# ── /interact student notification (background task) ──────────────────────────

async def _notify_student_of_status_change(
    student_slack_id: str,
    mentor_slack_id: str,
    date_str: str,
    status_label: str,
    hours: float,
) -> None:
    """DM the student when a mentor changes their session status."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        mentor_result = await db.execute(
            select(Mentor).where(Mentor.slack_user_id == mentor_slack_id)
        )
        mentor = mentor_result.scalars().first()
    mentor_name = mentor.name if mentor else "A mentor"
    await send_dm(
        student_slack_id,
        f"📝 *Session Updated*\n"
        f"Your session on {date_str} was changed to *{status_label}* "
        f"({hours:.2f} hrs) by {mentor_name}.\n"
        f"_If you haven't already, make sure to check in with them about this change._",
    )


# ── /shop helper ──────────────────────────────────────────────────────────────


def _build_shop_text(student_sessions, team_filter: Optional[int]) -> str:
    """Build the /shop roster message. team_filter is None, 4143, or 4423."""
    teams = [4143, 4423] if team_filter is None else [team_filter]
    lines = []

    total_students = 0
    for team_num in teams:
        team_students = [s for s in student_sessions if s.student.team.number == team_num]
        total_students += len(team_students)
        lines.append(f"*Team {team_num} — {len(team_students)} signed in*")
        if team_students:
            for s in team_students:
                lines.append(f"• {s.student.name} · {format_elapsed(s.sign_in_time)}")
        else:
            lines.append("  _Nobody signed in_")
        lines.append("")

    if team_filter is None:
        header = f"*{total_students} student{'s' if total_students != 1 else ''} in the shop*\n"
        return header + "\n".join(lines)

    return "\n".join(lines)


def _neighbor_gap_text(rows: list, my_sid: int, my_total: float) -> str:
    """
    Describe the hours gap to the students immediately above and below
    my_sid in the standings (rows must have .sid and .total). Ties are
    broken by sid for a stable neighbor pick; a 0.0 gap is reported as
    "tied" rather than "0.0 hrs".
    """
    ordered = sorted(rows, key=lambda r: (-r.total, r.sid))
    idx = next(i for i, r in enumerate(ordered) if r.sid == my_sid)

    parts = []
    if idx > 0:
        gap = ordered[idx - 1].total - my_total
        parts.append("tied with the spot above" if gap == 0
                      else f"{gap:.1f} hrs behind the spot above")
    if idx < len(ordered) - 1:
        gap = my_total - ordered[idx + 1].total
        parts.append("tied with the spot below" if gap == 0
                      else f"{gap:.1f} hrs ahead of the spot below")
    return " · ".join(parts)


# ── Slash command router ───────────────────────────────────────────────────────

@router.post("/command")
async def slack_command(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    await _verify_slack_signature(request)

    form = await request.form()
    command = form.get("command", "")
    text = (form.get("text") or "").strip()
    user_id = form.get("user_id", "")

    # ── /hours — inline response, visible only to caller ──
    if command == "/hours":
        week_start = today_local() - timedelta(days=today_local().weekday())
        week_start_utc, week_end_utc = current_week_bounds()
        week_str = week_start.strftime("%b %d")

        s_result = await db.execute(
            select(Student).where(Student.slack_user_id == user_id)
        )
        student = s_result.scalars().first()

        if student:
            season_result = await db.execute(
                select(sqlfunc.coalesce(sqlfunc.sum(AttendanceSession.hours_counted), 0.0))
                .where(
                    AttendanceSession.student_id == student.id,
                    AttendanceSession.sign_out_time.is_not(None),
                )
            )
            season_total = float(season_result.scalar() or 0.0)

            # Leaderboard rank across program teams — same all-time basis as season_total above.
            rank_rows = (await db.execute(
                select(
                    Student.id.label("sid"),
                    Student.team_id,
                    Team.number.label("team_number"),
                    sqlfunc.coalesce(sqlfunc.sum(AttendanceSession.hours_counted), 0.0).label("total"),
                )
                .join(Team, Team.id == Student.team_id)
                .join(
                    AttendanceSession,
                    and_(
                        AttendanceSession.student_id == Student.id,
                        AttendanceSession.sign_out_time.is_not(None),
                    ),
                    isouter=True,
                )
                .where(Team.number.in_([4143, 4423]))
                .where(Student.is_active.is_(True))
                .group_by(Student.id)
            )).all()
            overall_count = len(rank_rows)
            overall_rank = 1 + sum(1 for r in rank_rows if r.total > season_total)
            team_rows = [r for r in rank_rows if r.team_id == student.team_id]
            team_count = len(team_rows)
            team_rank = 1 + sum(1 for r in team_rows if r.total > season_total)
            team_number = next((r.team_number for r in rank_rows if r.sid == student.id), None)
            overall_gap = _neighbor_gap_text(rank_rows, student.id, season_total)
            team_gap = _neighbor_gap_text(team_rows, student.id, season_total)

            week_result = await db.execute(
                select(sqlfunc.coalesce(sqlfunc.sum(AttendanceSession.hours_counted), 0.0))
                .where(
                    AttendanceSession.student_id == student.id,
                    AttendanceSession.sign_out_time.is_not(None),
                    AttendanceSession.sign_in_time >= week_start_utc,
                    AttendanceSession.sign_in_time < week_end_utc,
                )
            )
            week_hours = float(week_result.scalar() or 0.0)

            required = await resolve_requirement(db, student.team_id, student.subteam_slug, week_start)

            on_track = week_hours >= required
            status_icon = "✅" if on_track else "⚠️"

            reply = (
                f"{status_icon} *Your Hours — Week of {week_str}*\n"
                f"This week: *{week_hours:.1f} / {required:.1f} hrs*\n"
                f"Season total: *{season_total:.1f} hrs*\n"
                f"Rank: *#{overall_rank} of {overall_count}* overall · "
                f"*#{team_rank} of {team_count}* on Team {team_number}"
            )
            if overall_gap:
                reply += f"\n_Overall: {overall_gap}_"
            if team_gap:
                reply += f"\n_Team: {team_gap}_"
            if on_track:
                reply += "\nYou're on track — great work! 💪"
            else:
                remaining = required - week_hours
                reply += f"\n_{remaining:.1f} hrs still needed — you may need to make up hours in the upcoming week._"

            return Response(content=reply, media_type="text/plain")

        m_result = await db.execute(
            select(Mentor).where(Mentor.slack_user_id == user_id)
        )
        mentor = m_result.scalars().first()

        if mentor:
            season_result = await db.execute(
                select(sqlfunc.coalesce(sqlfunc.sum(MentorSession.hours_counted), 0.0))
                .where(
                    MentorSession.mentor_id == mentor.id,
                    MentorSession.sign_out_time.is_not(None),
                )
            )
            season_total = float(season_result.scalar() or 0.0)

            # Leaderboard rank across all mentors — same all-time basis as season_total above.
            rank_rows = (await db.execute(
                select(
                    Mentor.id.label("mid"),
                    sqlfunc.coalesce(sqlfunc.sum(MentorSession.hours_counted), 0.0).label("total"),
                )
                .join(
                    MentorSession,
                    and_(
                        MentorSession.mentor_id == Mentor.id,
                        MentorSession.sign_out_time.is_not(None),
                    ),
                    isouter=True,
                )
                .where(Mentor.is_active.is_(True))
                .group_by(Mentor.id)
            )).all()
            overall_count = len(rank_rows)
            overall_rank = 1 + sum(1 for r in rank_rows if r.total > season_total)

            week_result = await db.execute(
                select(sqlfunc.coalesce(sqlfunc.sum(MentorSession.hours_counted), 0.0))
                .where(
                    MentorSession.mentor_id == mentor.id,
                    MentorSession.sign_out_time.is_not(None),
                    MentorSession.sign_in_time >= week_start_utc,
                    MentorSession.sign_in_time < week_end_utc,
                )
            )
            week_hours = float(week_result.scalar() or 0.0)

            reply = (
                f"🛠️ *Your Mentor Hours — Week of {week_str}*\n"
                f"This week: *{week_hours:.1f} hrs*\n"
                f"Season total: *{season_total:.1f} hrs*\n"
                f"Rank: *#{overall_rank} of {overall_count}* overall"
            )

            return Response(content=reply, media_type="text/plain")

        return Response(
            content="❌ Your Slack account isn't linked to a student or mentor record. Please ask a mentor.",
            media_type="text/plain",
        )

    # ── /shop — inline response, visible only to caller ──
    if command == "/shop":
        team_filter = None
        if text:
            if text not in ("4143", "4423"):
                return Response(
                    content="Usage: `/shop`, `/shop 4143`, or `/shop 4423`",
                    media_type="text/plain",
                )
            team_filter = int(text)
        student_sessions = await get_signed_in_students(db)
        return Response(
            content=_build_shop_text(student_sessions, team_filter),
            media_type="text/plain",
        )

    # ── /qr — DM the caller their own kiosk QR badge (so they can get a replacement
    # themselves if they lose it; works for both students and mentors) ──
    if command == "/qr":
        student_result = await db.execute(
            select(Student).where(Student.slack_user_id == user_id, Student.is_active.is_(True))
        )
        student = student_result.scalars().first()
        if student and (student.member_code or student.student_code):
            sent = await send_qr_dm(user_id, student.member_code or student.student_code, student.name)
            return Response(
                content="📬 Sent your QR badge to your DMs!" if sent
                else "❌ Couldn't send your QR badge — try again in a bit, or ask a mentor.",
                media_type="text/plain",
            )

        mentor_result = await db.execute(
            select(Mentor).where(Mentor.slack_user_id == user_id, Mentor.is_active.is_(True))
        )
        mentor = mentor_result.scalars().first()
        if mentor and (mentor.member_code or mentor.mentor_code):
            sent = await send_qr_dm(user_id, mentor.member_code or mentor.mentor_code, mentor.name)
            return Response(
                content="📬 Sent your QR badge to your DMs!" if sent
                else "❌ Couldn't send your QR badge — try again in a bit, or ask a mentor.",
                media_type="text/plain",
            )

        return Response(
            content="❌ Your Slack account isn't linked to a student or mentor record with a badge code yet. Please ask a mentor.",
            media_type="text/plain",
        )

    if command != "/edit":
        return Response(content="Unknown command.", media_type="text/plain")

    # ── /edit — ephemeral interactive message, no DM ──
    if not text:
        return Response(
            content="Usage: `/edit <student name>`",
            media_type="text/plain",
        )

    # Verify the caller is a known mentor
    mentor_result = await db.execute(
        select(Mentor).where(Mentor.slack_user_id == user_id)
    )
    if not mentor_result.scalars().first():
        return Response(
            content="❌ Only registered mentors can edit student sessions.",
            media_type="text/plain",
        )

    # Find student by partial name match across all active students
    lower = text.lower()
    students_result = await db.execute(
        select(Student)
        .options(selectinload(Student.team))
        .where(sqlfunc.lower(Student.name).like(f"%{lower}%"))
    )
    students = students_result.scalars().all()

    if len(students) == 0:
        return Response(
            content=f"No student found matching '{text}'.",
            media_type="text/plain",
        )
    elif len(students) == 1:
        student = students[0]
    else:
        exact = [s for s in students if s.name.lower() == lower]
        if len(exact) == 1:
            student = exact[0]
        else:
            names = ", ".join(s.name for s in students)
            return Response(
                content=f"Multiple students match '{text}': {names}. Please be more specific.",
                media_type="text/plain",
            )

    sessions_result = await db.execute(
        select(AttendanceSession)
        .where(
            AttendanceSession.student_id == student.id,
            AttendanceSession.sign_out_time.is_not(None),
        )
        .order_by(AttendanceSession.sign_out_time.desc())
        .limit(5)
    )
    sessions = sessions_result.scalars().all()
    if not sessions:
        return Response(
            content=f"No past sessions found for {student.name}.",
            media_type="text/plain",
        )

    return JSONResponse({
        "response_type": "ephemeral",
        "blocks": _edit_session_list_blocks(student, sessions),
        "text": f"Edit session for {student.name}",
    })


# ── Interactive actions handler ────────────────────────────────────────────────

@router.post("/interact")
async def slack_interact(
    request: Request,
    background_tasks: BackgroundTasks,
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
    mentor_slack_id = payload.get("user", {}).get("id", "")
    response_url = payload.get("response_url", "")

    from slack_sdk.webhook.async_client import AsyncWebhookClient

    # ── Step 1: mentor selected a session from the list ──
    if action_id.startswith("edit_select_"):
        try:
            session_id = int(session_id_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid session id")

        result = await db.execute(
            select(AttendanceSession)
            .options(selectinload(AttendanceSession.student).selectinload(Student.team))
            .where(AttendanceSession.id == session_id)
        )
        session = result.scalars().first()

        # Acknowledge immediately — outbound Slack webhook calls go in the background
        # so a slow network path to Slack can't cause a 500 visible to the user.
        if not session:
            background_tasks.add_task(
                AsyncWebhookClient(response_url).send,
                text="⚠️ Session not found.",
                replace_original=True,
            )
        else:
            blocks = _edit_status_blocks(session, session.student)
            background_tasks.add_task(
                AsyncWebhookClient(response_url).send,
                text=f"Edit session for {session.student.name}",
                blocks=blocks,
                replace_original=True,
            )
        return Response(status_code=200)

    # ── Step 2: mentor chose a new status ──
    if action_id not in ("edit_contributor", "edit_present", "edit_distraction"):
        return Response(status_code=200)

    try:
        session_id = int(session_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session id")

    status = (
        SessionStatus.contributor if action_id == "edit_contributor"
        else SessionStatus.present if action_id == "edit_present"
        else SessionStatus.distraction
    )

    session = await update_session_status(db, session_id, status)
    if not session:
        background_tasks.add_task(
            AsyncWebhookClient(response_url).send,
            text="⚠️ Session not found or not yet signed out.",
            replace_original=True,
        )
        return Response(status_code=200)

    await broadcaster.broadcast("update")

    student = session.student
    date_str = utc_to_local(session.sign_in_time).strftime("%b %d")
    status_label = _STATUS_LABELS[status]
    hours = session.hours_counted

    # Audit log — update_session_status already committed, so this is a second commit.
    mentor_result = await db.execute(
        select(Mentor).where(Mentor.slack_user_id == mentor_slack_id)
    )
    mentor = mentor_result.scalars().first()
    actor = mentor.name if mentor else mentor_slack_id
    await audit.record(
        db, request, "session.edit",
        f"{actor} changed {student.name}'s session ({date_str}) to {status_label} via Slack",
        entity_type="session", entity_id=session.id,
        actor=actor,
        detail={"student": student.name, "status": status.value, "hours": hours, "via": "slack"},
    )
    await db.commit()

    if student.slack_user_id:
        background_tasks.add_task(
            _notify_student_of_status_change,
            student.slack_user_id,
            mentor_slack_id,
            date_str,
            status_label,
            hours,
        )

    background_tasks.add_task(
        AsyncWebhookClient(response_url).send,
        text=f"Updated session for {student.name}",
        blocks=[{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ *Updated — {student.name}*\n"
                    f"{date_str}: Status changed to *{status_label}* · *{hours:.2f} hrs* recorded"
                ),
            },
        }],
        replace_original=True,
    )
    return Response(status_code=200)
