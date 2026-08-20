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
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import AttendanceSession, Mentor, MentorSession, SessionStatus, Student, Team
from app.services import audit
from app.services.app_settings import get_leaderboard_since, leaderboard_since_utc, get_auto_signout_session_minutes
from app.services.attendance import update_session_status, get_signed_in_students, get_signed_in_mentors, sign_out_all_open, mentor_sign_out_all_open
from app.services.broadcaster import broadcaster
from app.services.legion_auth import make_link_url
from app.services.requirements import resolve_requirement
from app.services.scheduler import _post_wall_of_shame
from app.services.slack_client import open_modal, send_dm, send_qr_dm
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
    SessionStatus.distraction: "Distraction",
}


_EDIT_CALLBACK = "edit_session"
_STATUS_ICONS = {
    SessionStatus.contributor: "✅",
    SessionStatus.present: "🔸",
    SessionStatus.distraction: "🚫",
}


def _status_choices() -> list[tuple[SessionStatus, str]]:
    """The 3 status options with their *live* hours percentage — Admin -> Settings can
    change contributor/present/distraction_multiplier at any time (`_status_multiplier`
    in `services/attendance.py`, which this modal's own submit handler calls), and a
    stale hardcoded "(50% hours)" would mislead a mentor about what they're about to
    apply. Built fresh per call, not a module-level constant, so an admin's change
    shows up on the very next `/edit` without a restart."""
    from app.services.attendance import _status_multiplier

    choices = []
    for status in (SessionStatus.contributor, SessionStatus.present, SessionStatus.distraction):
        pct = round(_status_multiplier(status) * 100)
        portion = "full hours" if pct == 100 else f"{pct}% hours"
        label = f"{_STATUS_ICONS[status]} {_STATUS_LABELS[status]} ({portion})"
        choices.append((status, label))
    return choices


def _edit_session_modal(student: Student, sessions: list) -> dict:
    """The /edit modal: pick which of the student's last 5 closed sessions to change,
    and what to change it to — both in one view, submitted together.

    Replaces a two-step button wizard (pick session -> pick status), each step its own
    full round trip that edited the same ephemeral message via `response_url`. A modal
    collapses that to one screen and one submit; `_handle_edit_session_submit` is the
    other half."""
    session_options = []
    for s in sessions:
        date_str = utc_to_local(s.sign_in_time).strftime("%b %d")
        status_label = _STATUS_LABELS.get(s.status, "—") if s.status else "—"
        label = f"{date_str} · {format_elapsed(s.sign_in_time, s.sign_out_time)} · {status_label}"
        session_options.append({
            "text": {"type": "plain_text", "text": label},
            "value": str(s.id),
        })

    status_options = [
        {"text": {"type": "plain_text", "text": label}, "value": status.value}
        for status, label in _status_choices()
    ]

    title = f"Edit — {student.name}"
    if len(title) > 24:  # Slack caps modal titles at 24 characters
        title = title[:23] + "…"

    return {
        "type": "modal",
        "callback_id": _EDIT_CALLBACK,
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "session",
                "label": {"type": "plain_text", "text": "Session"},
                "element": {
                    "type": "static_select",
                    "action_id": "value",
                    "placeholder": {"type": "plain_text", "text": "Choose a session"},
                    "options": session_options,
                },
            },
            {
                "type": "input",
                "block_id": "status",
                "label": {"type": "plain_text", "text": "New contribution level"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "value",
                    "options": status_options,
                },
            },
        ],
    }


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


def _build_shop_text(student_sessions, mentor_sessions, team_filter: Optional[int]) -> str:
    """Build the /shop roster message. team_filter is None, 4143, or 4423."""
    teams = [4143, 4423] if team_filter is None else [team_filter]
    lines = []

    for team_num in teams:
        team_students = [s for s in student_sessions if s.student.team.number == team_num]
        team_mentors = [
            m for m in mentor_sessions
            if (m.mentor.team.number if m.mentor.team else 4143) == team_num
        ]

        lines.append(
            f"*Team {team_num} — {len(team_students)} student{'s' if len(team_students) != 1 else ''}, "
            f"{len(team_mentors)} mentor{'s' if len(team_mentors) != 1 else ''} signed in*"
        )
        if team_students:
            for s in team_students:
                lines.append(f"• {s.student.name} · {format_elapsed(s.sign_in_time)}")
        else:
            lines.append("  _No students signed in_")
        if team_mentors:
            lines.append("_Mentors_")
            for m in team_mentors:
                lines.append(f"• {m.mentor.name} · {format_elapsed(m.sign_in_time)}")
        lines.append("")

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


def _ephemeral(text: str) -> JSONResponse:
    """Wrap `text` as an ephemeral (caller-only) Slack response, rendered as mrkdwn so a
    `<url|label>` link comes through clickable rather than as a raw URL."""
    return JSONResponse({
        "response_type": "ephemeral",
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    })


def _hours_response(reply: str, member_code: Optional[str]) -> JSONResponse:
    """Ephemeral /hours reply with a one-tap "open my dashboard" link appended (mirrors
    Munus's /vhours). A plain mrkdwn hyperlink, so it opens in the browser without firing
    a Slack interaction, and a signed magic link, so the tap works first time even in
    Slack's in-app browser where no cookie survives — safe because an ephemeral reply is
    visible only to the caller. Omitted when the member has no `member_code` (legacy-only
    rows Legion can't match)."""
    if member_code:
        link = f"<{make_link_url(member_code, '/me')}|📊 Open my dashboard>"
        reply = f"{reply}\n{link}"
    return _ephemeral(reply)


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
    trigger_id = form.get("trigger_id", "")

    # ── /tempus — a bare one-tap link to the personal dashboard, no stats ──
    if command == "/tempus":
        student = (
            await db.execute(
                select(Student).where(Student.slack_user_id == user_id, Student.is_active.is_(True))
            )
        ).scalars().first()
        member_code = student.member_code if student else None
        if member_code is None:
            mentor = (
                await db.execute(
                    select(Mentor).where(Mentor.slack_user_id == user_id, Mentor.is_active.is_(True))
                )
            ).scalars().first()
            member_code = mentor.member_code if mentor else None
        if member_code is None:
            return Response(
                content="❌ Your Slack account isn't linked to a Tempus record. Please ask a mentor.",
                media_type="text/plain",
            )
        # /me is open to both students and mentors (unlike Munus's, student-only) —
        # see legion/app/services/home.py's tiles_for for the same distinction.
        return _ephemeral(f"<{make_link_url(member_code, '/me')}|🕒 Open Tempus>")

    # ── /hours — inline response, visible only to caller ──
    if command == "/hours":
        week_start = today_local() - timedelta(days=today_local().weekday())
        week_start_utc, week_end_utc = current_week_bounds()
        week_str = week_start.strftime("%b %d")

        leaderboard_since = await get_leaderboard_since(db)
        since_utc = await leaderboard_since_utc(db)

        s_result = await db.execute(
            select(Student).where(Student.slack_user_id == user_id)
        )
        student = s_result.scalars().first()

        if student:
            season_q = (
                select(sqlfunc.coalesce(sqlfunc.sum(AttendanceSession.hours_counted), 0.0))
                .where(
                    AttendanceSession.student_id == student.id,
                    AttendanceSession.sign_out_time.is_not(None),
                )
            )
            if since_utc is not None:
                season_q = season_q.where(AttendanceSession.sign_in_time >= since_utc)
            season_result = await db.execute(season_q)
            season_total = float(season_result.scalar() or 0.0)

            # Leaderboard rank across program teams — same "counts since" basis as season_total above.
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
                    sqlfunc.coalesce(sqlfunc.sum(AttendanceSession.hours_counted), 0.0).label("total"),
                )
                .join(Team, Team.id == Student.team_id)
                .join(AttendanceSession, rank_join, isouter=True)
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
            if leaderboard_since:
                reply += f"\n_(Counting since {leaderboard_since.strftime('%b %d')})_"
            if on_track:
                reply += "\nYou're on track — great work! 💪"
            else:
                remaining = required - week_hours
                reply += f"\n_{remaining:.1f} hrs still needed — you may need to make up hours in the upcoming week._"

            return _hours_response(reply, student.member_code)

        m_result = await db.execute(
            select(Mentor).where(Mentor.slack_user_id == user_id)
        )
        mentor = m_result.scalars().first()

        if mentor:
            season_q = (
                select(sqlfunc.coalesce(sqlfunc.sum(MentorSession.hours_counted), 0.0))
                .where(
                    MentorSession.mentor_id == mentor.id,
                    MentorSession.sign_out_time.is_not(None),
                )
            )
            if since_utc is not None:
                season_q = season_q.where(MentorSession.sign_in_time >= since_utc)
            season_result = await db.execute(season_q)
            season_total = float(season_result.scalar() or 0.0)

            # Leaderboard rank across all mentors — same "counts since" basis as season_total above.
            rank_join = and_(
                MentorSession.mentor_id == Mentor.id,
                MentorSession.sign_out_time.is_not(None),
            )
            if since_utc is not None:
                rank_join = and_(rank_join, MentorSession.sign_in_time >= since_utc)
            rank_rows = (await db.execute(
                select(
                    Mentor.id.label("mid"),
                    sqlfunc.coalesce(sqlfunc.sum(MentorSession.hours_counted), 0.0).label("total"),
                )
                .join(MentorSession, rank_join, isouter=True)
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
            if leaderboard_since:
                reply += f"\n_(Counting since {leaderboard_since.strftime('%b %d')})_"

            return _hours_response(reply, mentor.member_code)

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
        mentor_sessions = await get_signed_in_mentors(db)
        return Response(
            content=_build_shop_text(student_sessions, mentor_sessions, team_filter),
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

    # ── /gtfo — mentor-only: sign every currently signed-in student *and mentor*
    # out right now (including the caller themself, if they're still signed in).
    # Requires an *active* mentor — this is more consequential than /edit (which only
    # checks slack_user_id match), so an archived mentor's stale Slack link shouldn't
    # still be able to trigger a mass sign-out.
    if command == "/gtfo":
        mentor_result = await db.execute(
            select(Mentor).where(Mentor.slack_user_id == user_id, Mentor.is_active.is_(True))
        )
        mentor = mentor_result.scalars().first()
        if not mentor:
            return Response(
                content="❌ Only registered mentors can sign everyone out.",
                media_type="text/plain",
            )

        minutes = await get_auto_signout_session_minutes(db)
        session_length = timedelta(minutes=minutes)
        closed = await sign_out_all_open(db, session_length=session_length)
        mentor_count = await mentor_sign_out_all_open(db, session_length=session_length)
        if closed:
            await broadcaster.broadcast("update")
        if mentor_count:
            await broadcaster.broadcast("mentor_update")

        await audit.record(
            db, request, "attendance.bulk_signout",
            f"{mentor.name} signed out {len(closed)} student(s) and {mentor_count} mentor(s) via /gtfo",
            entity_type="session",
            actor=mentor.name,
            detail={"count": len(closed), "mentor_count": mentor_count, "via": "slack"},
        )
        await db.commit()

        # Wall of Shame is student-only — mentors don't get roasted (see test_mentor_signout.py).
        await _post_wall_of_shame(closed)

        if not closed and not mentor_count:
            return Response(content="No one was signed in.", media_type="text/plain")
        parts = []
        if closed:
            parts.append(f"{len(closed)} student(s)")
        if mentor_count:
            parts.append(f"{mentor_count} mentor(s)")
        return Response(content=f"✅ Signed out {' and '.join(parts)}.", media_type="text/plain")

    if command != "/edit":
        return Response(content="Unknown command.", media_type="text/plain")

    # ── /edit — ephemeral interactive message, no DM ──
    if not text:
        return Response(
            content="Usage: `/edit <student name>`",
            media_type="text/plain",
        )

    # Verify the caller is a known, active mentor
    mentor_result = await db.execute(
        select(Mentor).where(Mentor.slack_user_id == user_id, Mentor.is_active.is_(True))
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
        .where(
            sqlfunc.lower(Student.name).like(f"%{lower}%"),
            Student.is_active.is_(True),
        )
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

    if not trigger_id:
        return Response(
            content="⚠️ Couldn't open the edit form — try again.", media_type="text/plain"
        )
    ok = await open_modal(trigger_id, _edit_session_modal(student, sessions))
    if not ok:
        return Response(
            content="⚠️ Couldn't open the edit form — try again in a bit.",
            media_type="text/plain",
        )
    return Response(status_code=200)


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

    if payload.get("type") == "view_submission":
        view = payload.get("view", {})
        if view.get("callback_id") == _EDIT_CALLBACK:
            return await _handle_edit_session_submit(
                request, db, background_tasks, view, payload.get("user", {}).get("id", "")
            )
        return Response(status_code=200)

    # No other interactive component exists in Tempus today (the /edit flow above is
    # the only one, and it's now a modal) — nothing currently reaches this point, kept
    # as a safe no-op rather than assuming block_actions.
    return Response(status_code=200)


def _selected_option_value(view: dict, block_id: str) -> str:
    """The `value` of whichever option is selected in `block_id` (a `static_select` or
    `radio_buttons` input), or "" if nothing was — same state shape either way."""
    values = view.get("state", {}).get("values", {})
    selected = values.get(block_id, {}).get("value", {}).get("selected_option") or {}
    return selected.get("value", "")


async def _handle_edit_session_submit(
    request: Request,
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    view: dict,
    mentor_slack_id: str,
) -> Response:
    """Submission of the /edit modal — apply the chosen session's new contribution
    level. Reuses `update_session_status`, the same call the old button flow made, so
    the hours math and audit/notify side effects are unchanged; only how the mentor
    gets there did.

    Re-verifies the caller is still an active mentor at submit time — the old button
    flow didn't (it trusted the check `/edit` made before ever showing buttons, and
    Slack scopes both an ephemeral message and a modal to the one user who triggered
    it either way) — but a modal can sit open for a while, and re-checking here is
    cheap insurance against a mentor being deactivated in between."""
    mentor_result = await db.execute(
        select(Mentor).where(Mentor.slack_user_id == mentor_slack_id, Mentor.is_active.is_(True))
    )
    mentor = mentor_result.scalars().first()
    if not mentor:
        return JSONResponse({
            "response_action": "errors",
            "errors": {"session": "You're no longer a registered mentor."},
        })

    try:
        session_id = int(_selected_option_value(view, "session"))
    except ValueError:
        return JSONResponse({"response_action": "errors", "errors": {"session": "Pick a session."}})

    try:
        status = SessionStatus(_selected_option_value(view, "status"))
    except ValueError:
        return JSONResponse({
            "response_action": "errors", "errors": {"status": "Pick a contribution level."},
        })

    session = await update_session_status(db, session_id, status)
    if not session:
        return JSONResponse({
            "response_action": "errors",
            "errors": {"session": "That session is no longer available, or hasn't been signed out yet."},
        })

    await broadcaster.broadcast("update")

    student = session.student
    date_str = utc_to_local(session.sign_in_time).strftime("%b %d")
    status_label = _STATUS_LABELS[status]
    hours = session.hours_counted

    # Audit log — update_session_status already committed, so this is a second commit.
    await audit.record(
        db, request, "session.edit",
        f"{mentor.name} changed {student.name}'s session ({date_str}) to {status_label} via Slack",
        entity_type="session", entity_id=session.id,
        actor=mentor.name,
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

    # Empty 200 closes the modal — Slack's own convention for "submission accepted".
    return Response(status_code=200)
