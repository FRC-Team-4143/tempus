"""
SSO identity — the signed `mw_sso` browser cookie shared across MARS/WARS apps.

Legion mints `mw_sso` once a member approves a Slack push; every sibling app verifies it
locally with the shared `sso_secret` — no callback to Legion needed. Tempus is a *consumer*:
it only ever **verifies** the cookie (it never mints one), so this is the verify half of
Legion's `services/sso.py`. Single sign-out is just a redirect to Legion's `/sso/logout`.

Claims carried by the cookie (see Legion's `make_sso_token`):
    member_code, username, name, role, team_number, groups (list of slugs), slack_user_id
`/admin` is gated on the `tempus-admin` slug being present in `groups`.
"""
from typing import Optional
from urllib.parse import urlparse

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

SSO_COOKIE = "mw_sso"

_sso_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")


def read_sso_token(token: Optional[str]) -> Optional[dict]:
    """The verified claims for a raw cookie value, or None if absent/invalid/expired."""
    if not token:
        return None
    try:
        return _sso_signer.loads(token, max_age=settings.sso_session_ttl)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def sso_identity(request: Request) -> Optional[dict]:
    """The verified SSO claims for the current request, or None if absent/invalid."""
    return read_sso_token(request.cookies.get(SSO_COOKIE))


def make_authorize_url(request: Request) -> str:
    """Where to send an unauthenticated caller to sign in: Legion's `/sso/authorize`,
    with `return_to` pointing back at the page they were trying to reach."""
    from urllib.parse import quote
    return_to = quote(str(request.url), safe="")
    return f"{settings.legion_base_url}/sso/authorize?app=tempus&return_to={return_to}"


def stepup_url(request: Request, *, return_to: Optional[str] = None) -> str:
    """Step a magic-link session up to a full one: Legion's `/sso/stepup` re-mints
    `mw_sso` WITH groups via a fresh Slack Approve/Deny — no sign-out first.

    Same shape as `make_authorize_url`, but a distinct Legion endpoint on purpose: a
    `via="link"` cookie that lands on `/sso/authorize` counts as "already signed in" and
    is bounced straight back, which — since the admin gate sends link identities here —
    would loop. `return_to` defaults to the current URL; pass a bare Tempus path to come
    back to that page instead (made absolute so Legion's redirect resolves against
    Tempus's host, not Legion's)."""
    from urllib.parse import quote
    if return_to is None:
        target = str(request.url)
    elif urlparse(return_to).netloc:
        target = return_to
    else:
        target = f"{request.url.scheme}://{request.url.netloc}{return_to}"
    return f"{settings.legion_base_url}/sso/stepup?app=tempus&return_to={quote(target, safe='')}"


def logout_url(request: Request, *, return_to: str = "/admin") -> str:
    """Legion's single-logout endpoint, returning to `return_to` (default: Tempus's
    /admin) afterward."""
    from urllib.parse import quote
    base = f"{request.url.scheme}://{request.url.netloc}{return_to}"
    return f"{settings.legion_base_url}/sso/logout?return_to={quote(base, safe='')}"


# ── Open-redirect guard (mirrors Legion's `allowed_return_to`) ────────────────────

def is_same_app_path(url: Optional[str]) -> bool:
    """True only for a safe same-app relative path (leading '/', not protocol-relative).
    Also rejects a leading '/\\', which some browsers normalize to '//' (mirrors
    Legion's `allowed_return_to`)."""
    if not url:
        return False
    parsed = urlparse(url)
    return (
        not parsed.netloc
        and url.startswith("/")
        and not url.startswith("//")
        and not url.startswith("/\\")
    )
