"""
Kiosk device pairing — which browsers are trusted to display the boards.

Replaces the old `signin_ip_whitelist` CIDR gate. Trust is bound to a *device*
rather than a network, so a display keeps working when the shop changes ISP or
travels to a competition, while a student's phone on the shop wifi does not get in.

Enrollment is the smart-TV device flow: an unpaired display shows a short `user_code`,
an admin matches it against the screen physically in front of them and approves. The
display's browser holds a signed `tempus_kiosk` cookie carrying an opaque `device_id`
the whole time — provisional while pending, working once approved. This is Tempus's
only minted cookie; `services/sso.py` merely verifies Legion's.

The cookie is signed but never authoritative: `current_device` re-reads the row on
every gated request, so a revoke takes effect immediately rather than at cookie expiry.
Rotating `sso_secret` invalidates every pairing at once — same mass-revoke lever as
`services/badge.py`, which shares the secret under a different domain separator.

Pairing is **closed by default**. The board pages must be publicly reachable (that is
where an unpaired display arrives), so an always-open enrollment endpoint would let
anyone who can reach BASE_URL create rows. An admin opens a short window instead; see
`pairing_open_until`. A row cap alone would not do — filling it would deny pairing to a
legitimate display, converting spam into a lockout.
"""
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import KioskDevice, KioskDeviceStatus
from app.services.app_settings import get_setting, set_setting

KIOSK_COOKIE = "tempus_kiosk"
KIOSK_PAIRING_OPEN_UNTIL_KEY = "kiosk_pairing_open_until"

# Salt domain-separates this signer from sso.py's "mw-sso" and badge.py's
# b"tempus-badge:" context prefix, all three of which key off `sso_secret`.
_COOKIE_SALT = "tempus-kiosk-device"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 730  # 2 years — devices last until revoked

PAIRING_WINDOW_MINUTES = 10
MAX_PENDING = 10  # cap on concurrent pending requests during an open window

# Per-IP throttle on pending-row creation. In-process, like broadcaster.py.
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW = timedelta(minutes=10)
_recent_requests: dict[str, list[datetime]] = {}

# Unambiguous alphabet — no 0/O/1/I/L, since someone reads this off a screen
# across the room and types nothing (they only compare it to the admin list).
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _signer() -> URLSafeTimedSerializer:
    """Built per call so a rotated/monkeypatched `sso_secret` takes effect at once
    (matching `badge.compute_badge_id`, not sso.py's import-time signer)."""
    return URLSafeTimedSerializer(settings.sso_secret, salt=_COOKIE_SALT)


def issue_cookie(response: Response, device_id: str) -> None:
    """Attach the signed device cookie to a response."""
    response.set_cookie(
        KIOSK_COOKIE,
        _signer().dumps({"did": device_id}),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=settings.base_url.startswith("https"),
    )


def read_device_id(request: Request) -> Optional[str]:
    """The verified `device_id` from the cookie, or None if absent/invalid/expired."""
    token = request.cookies.get(KIOSK_COOKIE)
    if not token:
        return None
    try:
        payload = _signer().loads(token, max_age=_COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    did = payload.get("did")
    return did if isinstance(did, str) else None


async def current_device(db: AsyncSession, request: Request) -> Optional[KioskDevice]:
    """The KioskDevice row this request's cookie names, whatever its status, or None."""
    device_id = read_device_id(request)
    if not device_id:
        return None
    return (
        await db.execute(select(KioskDevice).where(KioskDevice.device_id == device_id))
    ).scalars().first()


def client_ip(request: Request) -> str:
    """Real client IP (nginx sets X-Forwarded-For; uvicorn --proxy-headers applies it)."""
    return request.client.host if request.client else "unknown"


# --- pairing window ---------------------------------------------------------

async def pairing_open_until(db: AsyncSession) -> Optional[datetime]:
    """Naive-UTC instant the pairing window shuts, or None if pairing is closed.

    A stored time in the past reads the same as never-opened, so an admin who
    forgets to close the window is not leaving enrollment open indefinitely.
    """
    raw = await get_setting(db, KIOSK_PAIRING_OPEN_UNTIL_KEY)
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when > datetime.utcnow() else None


async def open_pairing(db: AsyncSession) -> datetime:
    """Open the pairing window; returns when it will shut."""
    until = datetime.utcnow() + timedelta(minutes=PAIRING_WINDOW_MINUTES)
    await set_setting(db, KIOSK_PAIRING_OPEN_UNTIL_KEY, until.isoformat())
    return until


async def close_pairing(db: AsyncSession) -> None:
    await set_setting(db, KIOSK_PAIRING_OPEN_UNTIL_KEY, None)


# --- pending-row lifecycle --------------------------------------------------

async def prune_pending(db: AsyncSession) -> None:
    """Drop pending rows older than one pairing window. Staged, not committed."""
    cutoff = datetime.utcnow() - timedelta(minutes=PAIRING_WINDOW_MINUTES)
    stale = (
        await db.execute(
            select(KioskDevice).where(
                KioskDevice.status == KioskDeviceStatus.pending,
                KioskDevice.created_at < cutoff,
            )
        )
    ).scalars().all()
    for row in stale:
        await db.delete(row)


async def pending_count(db: AsyncSession) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(KioskDevice)
            .where(KioskDevice.status == KioskDeviceStatus.pending)
        )
    ).scalar_one()


def rate_limit_ok(ip: str) -> bool:
    """True if this IP may create another pending row. Records the attempt."""
    now = datetime.utcnow()
    seen = [t for t in _recent_requests.get(ip, []) if now - t < _RATE_LIMIT_WINDOW]
    if len(seen) >= _RATE_LIMIT_MAX:
        _recent_requests[ip] = seen
        return False
    seen.append(now)
    _recent_requests[ip] = seen
    return True


def reset_rate_limit() -> None:
    """Clear the in-process throttle. For tests — the dict is module-global."""
    _recent_requests.clear()


async def generate_user_code(db: AsyncSession) -> str:
    """A short display code, unique among rows currently pending."""
    taken = set(
        (
            await db.execute(
                select(KioskDevice.user_code).where(
                    KioskDevice.status == KioskDeviceStatus.pending
                )
            )
        ).scalars().all()
    )
    while True:
        code = "MARS-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
        if code not in taken:
            return code


async def create_pending(db: AsyncSession, request: Request) -> KioskDevice:
    """Register a new pending pairing request. Staged, not committed."""
    device = KioskDevice(
        device_id=secrets.token_hex(16),
        user_code=await generate_user_code(db),
        status=KioskDeviceStatus.pending,
        created_at=datetime.utcnow(),
        created_ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:200] or None,
    )
    db.add(device)
    return device


async def touch(db: AsyncSession, device: KioskDevice, request: Request) -> None:
    """Record last-seen, but only when stale by >5min — the boards poll every 60s
    and reopen SSE on every blip, so writing each time would put this on the hot path."""
    now = datetime.utcnow()
    if device.last_seen_at and now - device.last_seen_at < timedelta(minutes=5):
        return
    device.last_seen_at = now
    device.last_seen_ip = client_ip(request)
    await db.commit()
