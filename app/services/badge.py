"""
Predetermined, unauthenticated badge-QR access (`routers/badge.py`, `/badge/<id>`) — a
bookmarkable link that shows a member's own kiosk QR badge without any Legion SSO
session, so viewing it never has to create a full-account login on a shared computer.

`badge_id` is a pure, deterministic function of the member's code and Tempus's own
`sso_secret` — nothing is stored or issued, so there's no token table to leak and no
per-member revoke; the only way to invalidate every badge link at once is rotating
`sso_secret` itself. Domain-separated from Legion's own itsdangerous "mw-sso" salt use
via a fixed context string, so this doesn't collide with (or need) a second secret.
"""
import hashlib
import hmac
import io
from typing import Union

import qrcode
from fastapi import Response

from app.config import settings
from app.models import Mentor, Student

_BADGE_HMAC_CONTEXT = b"tempus-badge:"
_BADGE_ID_LENGTH = 32  # hex chars (128 bits) truncated from the SHA-256 digest


def effective_code(person: Union[Student, Mentor]) -> str:
    """The kiosk sign-in identity for a Student or Mentor row: Legion's `member_code`,
    falling back to the legacy `student_code`/`mentor_code` for rows that predate it."""
    if isinstance(person, Student):
        return person.member_code or person.student_code
    return person.member_code or person.mentor_code


def compute_badge_id(code: str) -> str:
    return hmac.new(
        settings.sso_secret.encode(), _BADGE_HMAC_CONTEXT + code.encode(), hashlib.sha256
    ).hexdigest()[:_BADGE_ID_LENGTH]


def qr_png_response(code: str) -> Response:
    img = qrcode.make(code)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")
