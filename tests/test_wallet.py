"""Apple Wallet (.pkpass) + Google Wallet passes for the kiosk badge.

The feature is dormant until configured (routes 404); once configured, an unmatched
badge id must be indistinguishable from a real one (mirrors tests/test_badge.py).
Signing material is generated on the fly here — no real Apple/Google account needed.
"""
import datetime
import hashlib
import io
import json
import zipfile

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

import app.services.wallet as wallet_mod
from app.config import settings
from app.services.badge import compute_badge_id
from app.services.wallet import Badgeholder, build_pkpass, google_save_url


_WALLET_SETTINGS = (
    "apple_wallet_p12_path", "apple_wallet_p12_password", "apple_wallet_wwdr_path",
    "apple_wallet_pass_type_id", "apple_wallet_team_id",
    "google_wallet_service_account_path", "google_wallet_issuer_id",
)


@pytest.fixture(autouse=True)
def _wallet_unconfigured(monkeypatch):
    """Start every test with the feature OFF regardless of the dev's real .env; the
    `apple_wallet` / `google_wallet` fixtures opt back in with throwaway creds."""
    for name in _WALLET_SETTINGS:
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "apple_wallet_org_name", "Tempus")
    wallet_mod._load_apple_credentials.cache_clear()
    wallet_mod._load_service_account.cache_clear()


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _self_signed(cn, key):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )


@pytest.fixture
def apple_wallet(tmp_path, monkeypatch):
    wallet_mod._load_apple_credentials.cache_clear()
    key = _key()
    cert = _self_signed("Pass Type ID: pass.test.tempus", key)
    wwdr_key = _key()
    wwdr = _self_signed("Apple Worldwide Developer Relations CA - G4", wwdr_key)

    p12 = tmp_path / "pass.p12"
    p12.write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"tempus", key, cert, None, serialization.BestAvailableEncryption(b"pw123")
        )
    )
    wwdr_pem = tmp_path / "wwdr.pem"
    wwdr_pem.write_bytes(wwdr.public_bytes(serialization.Encoding.PEM))

    monkeypatch.setattr(settings, "apple_wallet_p12_path", str(p12))
    monkeypatch.setattr(settings, "apple_wallet_p12_password", "pw123")
    monkeypatch.setattr(settings, "apple_wallet_wwdr_path", str(wwdr_pem))
    monkeypatch.setattr(settings, "apple_wallet_pass_type_id", "pass.test.tempus")
    monkeypatch.setattr(settings, "apple_wallet_team_id", "TEAM123456")
    monkeypatch.setattr(settings, "apple_wallet_org_name", "MARS/WARS")
    yield
    wallet_mod._load_apple_credentials.cache_clear()


@pytest.fixture
def google_wallet(tmp_path, monkeypatch):
    wallet_mod._load_service_account.cache_clear()
    key = _key()
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    sa = tmp_path / "sa.json"
    sa.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "svc@tempus-test.iam.gserviceaccount.com",
                "private_key": priv_pem,
                "private_key_id": "kid-test-1",
            }
        )
    )
    monkeypatch.setattr(settings, "google_wallet_service_account_path", str(sa))
    monkeypatch.setattr(settings, "google_wallet_issuer_id", "3388000000012345")
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    yield pub_pem
    wallet_mod._load_service_account.cache_clear()


# ─────────────────────────── dormant until configured ───────────────────────

async def test_routes_404_when_unconfigured(client, db, make_student):
    await make_student(code="ada00001")
    bid = compute_badge_id("ada00001")
    assert (await client.get(f"/wallet/apple/{bid}.pkpass")).status_code == 404
    assert (await client.get(f"/wallet/google/{bid}")).status_code == 404


async def test_badge_page_has_no_wallet_buttons_when_unconfigured(client, db, make_student):
    await make_student(code="ada00001")
    resp = await client.get(f"/badge/{compute_badge_id('ada00001')}")
    assert "Add to Apple Wallet" not in resp.text
    assert "Add to Google Wallet" not in resp.text


# ─────────────────────────────── Apple Wallet ───────────────────────────────

def _unzip(data):
    return zipfile.ZipFile(io.BytesIO(data))


def test_build_pkpass_structure_and_manifest(apple_wallet):
    holder = Badgeholder(name="Ada Lovelace", code="ada00001", role="student",
                         team_number=4143, subteam_label="Software")
    archive = _unzip(build_pkpass(holder))
    names = set(archive.namelist())
    assert {"pass.json", "manifest.json", "signature", "icon.png", "icon@2x.png"} <= names

    meta = json.loads(archive.read("pass.json"))
    assert meta["serialNumber"] == compute_badge_id("ada00001")
    assert meta["barcodes"][0]["message"] == "ada00001"
    assert meta["barcodes"][0]["format"] == "PKBarcodeFormatQR"
    assert meta["passTypeIdentifier"] == "pass.test.tempus"
    assert meta["teamIdentifier"] == "TEAM123456"
    assert meta["generic"]["primaryFields"][0]["value"] == "Ada Lovelace"

    manifest = json.loads(archive.read("manifest.json"))
    for fname, digest in manifest.items():
        assert hashlib.sha1(archive.read(fname)).hexdigest() == digest
    assert "signature" not in manifest and "manifest.json" not in manifest


def test_pkpass_signature_is_der_pkcs7(apple_wallet):
    from cryptography.hazmat.primitives.serialization import pkcs7

    archive = _unzip(build_pkpass(Badgeholder(name="Ada", code="ada00001")))
    sig = archive.read("signature")
    assert sig[:1] == b"\x30"  # DER SEQUENCE
    # signer cert + the WWDR intermediate we added should both be embedded
    assert len(pkcs7.load_der_pkcs7_certificates(sig)) >= 1


async def test_apple_route_serves_pkpass(client, db, make_student, apple_wallet):
    await make_student(name="Ada Lovelace", code="ada00001")
    resp = await client.get(f"/wallet/apple/{compute_badge_id('ada00001')}.pkpass")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.apple.pkpass"
    meta = json.loads(_unzip(resp.content).read("pass.json"))
    assert meta["barcodes"][0]["message"] == "ada00001"
    assert meta["generic"]["primaryFields"][0]["value"] == "Ada Lovelace"


async def test_apple_route_real_and_garbage_ids_are_indistinguishable(client, db, make_student, apple_wallet):
    await make_student(name="Ada Lovelace", code="ada00001")
    real = await client.get(f"/wallet/apple/{compute_badge_id('ada00001')}.pkpass")
    fake = await client.get("/wallet/apple/not-a-real-id-at-all.pkpass")

    assert real.status_code == fake.status_code == 200
    assert real.headers["content-type"] == fake.headers["content-type"]
    fake_meta = json.loads(_unzip(fake.content).read("pass.json"))
    assert fake_meta["generic"]["primaryFields"][0]["value"] == "Tempus Member"
    assert "Ada Lovelace" not in fake.text


# ─────────────────────────────── Google Wallet ──────────────────────────────

async def test_google_route_redirects_to_signed_save_link(client, db, make_student, google_wallet):
    pub_pem = google_wallet
    await make_student(name="Ada Lovelace", code="ada00001")

    resp = await client.get(f"/wallet/google/{compute_badge_id('ada00001')}")
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://pay.google.com/gp/v/save/")

    token = loc.rsplit("/", 1)[1]
    claims = jwt.decode(token, pub_pem, algorithms=["RS256"], audience="google")
    assert claims["typ"] == "savetowallet"
    obj = claims["payload"]["genericObjects"][0]
    assert obj["barcode"]["value"] == "ada00001"
    assert obj["id"].endswith(compute_badge_id("ada00001"))
    assert obj["header"]["defaultValue"]["value"] == "Ada Lovelace"


def test_google_save_url_omits_missing_team(google_wallet):
    url = google_save_url(Badgeholder(name="No Team", code="xyz00009"))
    token = url.rsplit("/", 1)[1]
    obj = jwt.decode(token, options={"verify_signature": False})["payload"]["genericObjects"][0]
    assert "subheader" not in obj


# ─────────────────────────── badge page buttons ─────────────────────────────

async def test_badge_page_shows_buttons_when_configured(client, db, make_student, apple_wallet, google_wallet):
    await make_student(code="ada00001")
    resp = await client.get(f"/badge/{compute_badge_id('ada00001')}")
    assert "Add to Apple Wallet" in resp.text
    assert "Add to Google Wallet" in resp.text
    assert f"/wallet/apple/{compute_badge_id('ada00001')}.pkpass" in resp.text
