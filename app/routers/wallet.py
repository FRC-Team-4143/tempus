"""
`/wallet/apple/<id>.pkpass` and `/wallet/google/<id>` — unauthenticated, predetermined
per-member wallet passes carrying the same kiosk QR as `/badge/<id>`. See
`app/services/wallet.py` for why (native passes auto-max screen brightness) and
`app/services/badge.py` for the anti-oracle design this mirrors: an unmatched id must be
indistinguishable from a real one, so both routes still produce a plausible pass on a
miss rather than 404-ing only for fakes.

Both routes 404 outright when the platform isn't configured — that's a deployment fact,
not a per-id signal, so it leaks nothing.
"""
import hmac

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Mentor, Student, Subteam, Team
from app.services.badge import compute_badge_id, effective_code
from app.services.wallet import (
    Badgeholder,
    apple_wallet_configured,
    build_pkpass,
    google_save_url,
    google_wallet_configured,
)

router = APIRouter()


async def _resolve_holder(db: AsyncSession, badge_id: str) -> Badgeholder:
    """Walk every active Student and Mentor, comparing each one's badge id with
    `hmac.compare_digest` without early-exiting on a match — same constant-time walk as
    `badge._resolve_code`. On a miss, return a synthetic placeholder whose barcode is the
    32-char `badge_id` itself (matches no member at the kiosk, so it's inert) so a real
    vs. fake id can't be told apart by status code or content type."""
    students = (
        await db.execute(select(Student).where(Student.is_active.is_(True)))
    ).scalars().all()
    mentors = (
        await db.execute(select(Mentor).where(Mentor.is_active.is_(True)))
    ).scalars().all()

    match = None
    for person in (*students, *mentors):
        code = effective_code(person)
        if code and hmac.compare_digest(compute_badge_id(code), badge_id):
            match = person
    if match is None:
        return Badgeholder(name="Tempus Member", code=badge_id)

    team_number = None
    if match.team_id:
        team_number = (
            await db.execute(select(Team.number).where(Team.id == match.team_id))
        ).scalar()
    subteam_label = None
    if match.subteam_slug:
        subteam_label = (
            await db.execute(select(Subteam.label).where(Subteam.slug == match.subteam_slug))
        ).scalar() or match.subteam_slug.replace("-", " ").title()

    return Badgeholder(
        name=match.name,
        code=effective_code(match),
        role="student" if isinstance(match, Student) else "mentor",
        team_number=team_number,
        subteam_label=subteam_label,
    )


@router.get("/wallet/apple/{badge_id}.pkpass")
async def wallet_apple(badge_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    if not apple_wallet_configured():
        return Response(status_code=404)
    holder = await _resolve_holder(db, badge_id)
    return Response(
        content=build_pkpass(holder),
        media_type="application/vnd.apple.pkpass",
        headers={"Content-Disposition": 'attachment; filename="tempus-badge.pkpass"'},
    )


@router.get("/wallet/google/{badge_id}")
async def wallet_google(badge_id: str, db: AsyncSession = Depends(get_db)) -> Response:
    if not google_wallet_configured():
        return Response(status_code=404)
    holder = await _resolve_holder(db, badge_id)
    return RedirectResponse(google_save_url(holder), status_code=302)
