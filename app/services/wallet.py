"""
Apple Wallet (`.pkpass`) and Google Wallet ("Add to Google Wallet") badge passes —
a member's kiosk QR badge carried as a native wallet pass.

Why: whenever a pass with a barcode is on screen, both Apple Wallet and Google Wallet
automatically raise the phone to full screen brightness, so the kiosk scanner reads the
code on the first try (a screenshot or a web page doesn't do that).

The pass barcode is exactly `effective_code(person)` — the same bare member code the
web QR at `/badge/<id>` encodes (see `app/services/badge.py`) — so the kiosk needs no
changes. Passes are **static**: built and signed entirely in-process, no pass web
service, no APNs, no Google REST calls, no stored state. If a member's name or team
changes they re-add the pass.

Everything here is dormant until configured: `apple_wallet_configured()` /
`google_wallet_configured()` gate the routes in `app/routers/wallet.py`.
"""
import functools
import hashlib
import io
import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12

from app.config import settings
from app.services.badge import compute_badge_id

# CWD-relative, matching `StaticFiles(directory="static")` in app/main.py.
_WALLET_ASSET_DIR = Path("static/wallet")
_APPLE_IMAGE_FILES = ("icon.png", "icon@2x.png", "icon@3x.png", "logo.png", "logo@2x.png")

_GOOGLE_SAVE_URL = "https://pay.google.com/gp/v/save/"


class WalletNotConfigured(RuntimeError):
    """Raised if a build is attempted while the platform's settings are incomplete."""


@dataclass(frozen=True)
class Badgeholder:
    """The handful of facts a pass shows. Decoupled from the ORM so pass building is
    sync and trivially testable; `app/routers/wallet.py` builds one from the roster."""

    name: str
    code: str
    role: str = "member"  # "student" | "mentor" | "member" (placeholder on a lookup miss)
    team_number: Optional[int] = None
    subteam_label: Optional[str] = None

    @property
    def serial(self) -> str:
        """Stable, non-guessable pass id — the same HMAC used in `/badge/<id>` URLs."""
        return compute_badge_id(self.code)


# ─────────────────────────── configuration checks ────────────────────────────

def apple_wallet_configured() -> bool:
    s = settings
    if not (
        s.apple_wallet_pass_type_id
        and s.apple_wallet_team_id
        and s.apple_wallet_p12_path
        and s.apple_wallet_wwdr_path
    ):
        return False
    return Path(s.apple_wallet_p12_path).is_file() and Path(s.apple_wallet_wwdr_path).is_file()


def google_wallet_configured() -> bool:
    s = settings
    if not (s.google_wallet_issuer_id and s.google_wallet_service_account_path):
        return False
    return Path(s.google_wallet_service_account_path).is_file()


# ─────────────────────────────── Apple Wallet ────────────────────────────────

@functools.lru_cache(maxsize=4)
def _load_apple_credentials(p12_path: str, p12_password: str, wwdr_path: str):
    """Parse the signing cert/key + WWDR intermediate once per distinct path set. A
    prod cert swap keeps the same path, so it needs a process restart to pick up — the
    same constraint `sso_secret` already has (see CLAUDE.md)."""
    key, cert, _extra = pkcs12.load_key_and_certificates(
        Path(p12_path).read_bytes(),
        p12_password.encode() if p12_password else None,
    )
    wwdr_bytes = Path(wwdr_path).read_bytes()
    try:
        wwdr = x509.load_pem_x509_certificate(wwdr_bytes)
    except ValueError:
        wwdr = x509.load_der_x509_certificate(wwdr_bytes)
    return key, cert, wwdr


def _apple_credentials():
    return _load_apple_credentials(
        settings.apple_wallet_p12_path,
        settings.apple_wallet_p12_password,
        settings.apple_wallet_wwdr_path,
    )


def _apple_pass_json(holder: Badgeholder) -> dict:
    barcode = {
        "format": "PKBarcodeFormatQR",
        "message": holder.code,
        "messageEncoding": "iso-8859-1",
        "altText": holder.code,
    }
    secondary = []
    if holder.team_number is not None:
        secondary.append({"key": "team", "label": "TEAM", "value": str(holder.team_number)})
    if holder.subteam_label:
        secondary.append({"key": "subteam", "label": "SUBTEAM", "value": holder.subteam_label})

    return {
        "formatVersion": 1,
        "passTypeIdentifier": settings.apple_wallet_pass_type_id,
        "teamIdentifier": settings.apple_wallet_team_id,
        "organizationName": settings.apple_wallet_org_name or "Tempus",
        "description": "Tempus kiosk sign-in badge",
        "serialNumber": holder.serial,
        "logoText": "Tempus",
        "foregroundColor": "rgb(255, 255, 255)",
        "backgroundColor": "rgb(10, 10, 10)",
        "labelColor": "rgb(204, 34, 0)",
        # `barcodes` is the modern (iOS 9+) key; the singular `barcode` is kept for
        # older iOS. Both carry the identical bare member code the kiosk expects.
        "barcodes": [barcode],
        "barcode": barcode,
        "generic": {
            "primaryFields": [{"key": "name", "label": "MEMBER", "value": holder.name}],
            "secondaryFields": secondary,
            "backFields": [
                {"key": "code", "label": "Badge code", "value": holder.code},
                {
                    "key": "about",
                    "label": "About",
                    "value": (
                        "Scan the QR at the shop kiosk to sign in and out. Your phone "
                        "raises its screen brightness automatically while this pass is "
                        "open, so the scanner reads it on the first try."
                    ),
                },
            ],
        },
    }


def _sign_manifest(manifest_bytes: bytes) -> bytes:
    key, cert, wwdr = _apple_credentials()
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(manifest_bytes)
        .add_signer(cert, key, hashes.SHA256())
        .add_certificate(wwdr)
        .sign(
            serialization.Encoding.DER,
            [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary],
        )
    )


def build_pkpass(holder: Badgeholder) -> bytes:
    """The signed `.pkpass` archive bytes for `holder`. Raises `WalletNotConfigured`
    if Apple settings are incomplete (the route checks first, so that's defensive)."""
    if not apple_wallet_configured():
        raise WalletNotConfigured("apple")

    files: dict[str, bytes] = {
        "pass.json": json.dumps(_apple_pass_json(holder), indent=2).encode(),
    }
    for name in _APPLE_IMAGE_FILES:
        path = _WALLET_ASSET_DIR / name
        if path.is_file():
            files[name] = path.read_bytes()
    if "icon.png" not in files:
        raise WalletNotConfigured("apple: static/wallet/icon.png is missing")

    # manifest.json: SHA-1 of every other file (SHA-1 is fixed by Apple's spec here).
    manifest = {name: hashlib.sha1(data).hexdigest() for name, data in files.items()}
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    files["manifest.json"] = manifest_bytes
    files["signature"] = _sign_manifest(manifest_bytes)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buf.getvalue()


# ─────────────────────────────── Google Wallet ───────────────────────────────

@functools.lru_cache(maxsize=4)
def _load_service_account(path: str) -> Tuple[str, str, Optional[str]]:
    data = json.loads(Path(path).read_text())
    return data["client_email"], data["private_key"], data.get("private_key_id")


def _google_generic_object(holder: Badgeholder, class_id: str) -> dict:
    # Kept deliberately lean: the whole object is inlined in the save-link JWT, and
    # Google truncates save URLs past ~1800 chars. Skip nice-to-haves (logo alt text,
    # a redundant "badge code" row — it's already the barcode's visible value).
    obj = {
        "id": f"{settings.google_wallet_issuer_id}.{holder.serial}",
        "classId": class_id,
        "state": "ACTIVE",
        "hexBackgroundColor": "#0a0a0a",
        "logo": {"sourceUri": {"uri": f"{settings.base_url}/static/wallet/logo@2x.png"}},
        "cardTitle": {"defaultValue": {"language": "en", "value": "Tempus"}},
        "header": {"defaultValue": {"language": "en", "value": holder.name}},
        "barcode": {"type": "QR_CODE", "value": holder.code, "alternateText": holder.code},
    }
    if holder.team_number is not None:
        obj["subheader"] = {
            "defaultValue": {"language": "en", "value": f"Team {holder.team_number}"}
        }
    if holder.subteam_label:
        obj["textModulesData"] = [
            {"id": "subteam", "header": "Subteam", "body": holder.subteam_label}
        ]
    return obj


def google_save_url(holder: Badgeholder) -> str:
    """A signed "Add to Google Wallet" URL. The JWT inlines a minimal generic class +
    the object (a "fat" JWT), so Google creates both on tap — Tempus never calls the
    Wallet REST API. Keep the object small so the URL stays well under ~1.8 KB."""
    if not google_wallet_configured():
        raise WalletNotConfigured("google")

    client_email, private_key, kid = _load_service_account(
        settings.google_wallet_service_account_path
    )
    class_id = f"{settings.google_wallet_issuer_id}.tempus_badge"
    claims = {
        "iss": client_email,
        "aud": "google",
        "typ": "savetowallet",
        "iat": int(time.time()),
        "origins": [settings.base_url],
        "payload": {
            "genericClasses": [{"id": class_id}],
            "genericObjects": [_google_generic_object(holder, class_id)],
        },
    }
    token = jwt.encode(
        claims, private_key, algorithm="RS256", headers={"kid": kid} if kid else None
    )
    return _GOOGLE_SAVE_URL + token
