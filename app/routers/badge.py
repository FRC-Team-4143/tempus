"""
`/badge/<id>` — unauthenticated, predetermined per-member QR badge view. See
`app/services/badge.py` for the derivation and the anti-oracle design rationale: an
unmatched id must be indistinguishable from a real one (same status, same page shape,
same image content-type), so the route can't be used to probe which ids are real.
"""
import hmac

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Mentor, Student
from app.services.badge import compute_badge_id, effective_code, qr_png_response

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


async def _resolve_code(db: AsyncSession, badge_id: str) -> str:
    """Walk every active Student and Mentor, computing each one's badge id and
    comparing with `hmac.compare_digest` — without early-exiting on a match — so a hit
    and a total miss touch the same amount of work and can't be told apart by timing.
    Falls back to `badge_id` itself on a miss (guaranteed non-colliding with a real
    `member_code`/legacy code — different length/format), so the caller always has
    something plausible to encode."""
    students = (await db.execute(select(Student).where(Student.is_active.is_(True)))).scalars().all()
    mentors = (await db.execute(select(Mentor).where(Mentor.is_active.is_(True)))).scalars().all()

    match = None
    for person in (*students, *mentors):
        code = effective_code(person)
        if code and hmac.compare_digest(compute_badge_id(code), badge_id):
            match = code
    return match or badge_id


# Registered before the plain "/badge/{badge_id}" route below: FastAPI/Starlette
# matches routes in registration order, and the default string path converter allows
# dots — so "/badge/{badge_id}" would also match "/badge/abc.png" if it came first.
@router.get("/badge/{badge_id}.png")
async def badge_png(badge_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    code = await _resolve_code(db, badge_id)
    return qr_png_response(code)


@router.get("/badge/{badge_id}", response_class=HTMLResponse)
async def badge_page(badge_id: str, request: Request):
    """No DB/auth work at all — the page never varies by whether `badge_id` is real,
    so there's nothing to look up yet at this step."""
    return templates.TemplateResponse(
        "portal/badge.html", {"request": request, "badge_id": badge_id}
    )
