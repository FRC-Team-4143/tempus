"""
Kiosk routes — sign-in page, badge POST, and SSE stream.
"""
import ipaddress
import json
from datetime import datetime
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.schemas import SignInRequest, SignInResponse
from app.services.attendance import sign_in, get_signed_in_students
from app.services.broadcaster import broadcaster

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _is_allowed_ip(request: Request) -> bool:
    """Return True if IP whitelisting is disabled or the client IP is in an allowed CIDR."""
    whitelist_str = settings.signin_ip_whitelist.strip()
    if not whitelist_str:
        return True
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        addr = ipaddress.ip_address(client_ip)
        for cidr in whitelist_str.split(","):
            if addr in ipaddress.ip_network(cidr.strip(), strict=False):
                return True
    except ValueError:
        pass
    return False


def _format_sessions(sessions) -> dict:
    """Return signed-in students grouped by team number."""
    by_team: dict[int, list[dict]] = {}
    for s in sessions:
        team_number = s.student.team.number
        if team_number not in by_team:
            by_team[team_number] = []
        elapsed = datetime.utcnow() - s.sign_in_time
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes = remainder // 60
        by_team[team_number].append(
            {
                "session_id": s.id,
                "name": s.student.name,
                "sign_in_time": s.sign_in_time.strftime("%I:%M %p"),
                "elapsed": f"{hours}h {minutes:02d}m",
            }
        )
    return by_team


@router.get("/kiosk", response_class=HTMLResponse)
async def kiosk_page(request: Request, db: AsyncSession = Depends(get_db)):
    sessions = await get_signed_in_students(db)
    by_team = _format_sessions(sessions)
    return templates.TemplateResponse(
        "kiosk.html",
        {
            "request": request,
            "by_team": by_team,
            "teams": [4143, 4423],
        },
    )


@router.post("/kiosk/signin", response_model=SignInResponse)
async def kiosk_signin(
    body: SignInRequest, request: Request, db: AsyncSession = Depends(get_db)
):
    if not _is_allowed_ip(request):
        return SignInResponse(success=False, message="Sign-in not allowed from this location.")

    success, message, student = await sign_in(db, body.name.strip())
    if success:
        await broadcaster.broadcast("update")
    return SignInResponse(
        success=success,
        message=message,
        student_name=student.name if student else None,
        team_name=student.team.name if student else None,
    )


@router.get("/kiosk/data")
async def kiosk_data(db: AsyncSession = Depends(get_db)):
    """JSON snapshot of currently signed-in students, grouped by team number."""
    sessions = await get_signed_in_students(db)
    by_team = _format_sessions(sessions)
    # Ensure both teams are always present in the response
    for t in [4143, 4423]:
        by_team.setdefault(t, [])
    return by_team


@router.get("/kiosk/stream")
async def kiosk_stream():
    """Server-Sent Events endpoint — pushes 'update' events to all connected kiosks."""

    async def event_generator() -> AsyncGenerator[str, None]:
        q = broadcaster.subscribe()
        try:
            # Initial ping so the browser knows the connection is alive
            yield ": connected\n\n"
            while True:
                event = await q.get()
                yield f"event: {event}\ndata: \n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
