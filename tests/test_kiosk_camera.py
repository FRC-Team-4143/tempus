"""The kiosk webcam QR scanner (/kiosk).

A camera on the kiosk PC watches for QR badges on students' phone screens and
feeds the same `POST /kiosk/signin` pipeline as the handheld wedge scanner,
which keeps working as a fallback for printed badges and a dead webcam. There
is no JS test tooling in this project (no package.json, no browser runner), so
the actual scan/decode/debounce behaviour is verified by hand — see the
kiosk-camera checklist in the plan this landed from. What's asserted here is
everything the *server* controls or ships: that the library is vendored rather
than pulled from a CDN, that it's the UMD build loaded as a classic script (the
ES-module build would be deferred and evaluate after the inline controller that
calls into it), that the worker sits where the library's relative dynamic
import will look for it, that the two scanners share one submit path rather
than duplicating it, and that the ungated /kiosk/demo and the pinned boards
never start a camera.
"""
from pathlib import Path

import pytest

VENDOR = Path("static/vendor/qr-scanner")
BOARDS_JS = Path("static/kiosk-boards.js")
KIOSK_CSS = Path("static/kiosk.css")


# ── What ships ──────────────────────────────────────────────────────────────

def test_qr_scanner_is_vendored_not_pulled_from_a_cdn():
    """A shop display that can't reach jsDelivr must still be able to sign
    people in. Everything else on these pages is CDN-hosted; this deliberately
    isn't."""
    assert (VENDOR / "qr-scanner.umd.min.js").exists()
    assert "cdn.jsdelivr.net/npm/qr-scanner" not in BOARDS_JS.read_text()


def test_vendored_worker_sits_next_to_the_umd_build():
    """qr-scanner reaches its worker with `import('./qr-scanner-worker.min.js')`,
    which for a classic script resolves against the script's own URL — so the
    two files have to live in the same directory. QrScanner.WORKER_PATH is a
    no-op in 1.4.x and can't be used to point elsewhere."""
    umd = VENDOR / "qr-scanner.umd.min.js"
    assert (VENDOR / "qr-scanner-worker.min.js").exists()
    assert "./qr-scanner-worker.min.js" in umd.read_text()


def test_vendored_library_carries_its_license():
    text = (VENDOR / "LICENSE").read_text()
    assert "MIT" in text
    assert "Nimiq" in text


@pytest.mark.parametrize("name", ["qr-scanner.umd.min.js", "qr-scanner-worker.min.js"])
async def test_vendored_files_are_actually_served(client, name):
    """StaticFiles is mounted on a path relative to the CWD and the Dockerfile
    copies static/ wholesale — this catches a vendored file that was added but
    never staged, which would otherwise only show up as a dead camera in the
    shop."""
    resp = await client.get(f"/static/vendor/qr-scanner/{name}")
    assert resp.status_code == 200


# ── How the page loads it ────────────────────────────────────────────────────

async def test_kiosk_loads_the_umd_build_as_a_classic_script(paired_client):
    """The ES-module build would be deferred and would run *after* the inline
    script that calls Kiosk.initCameraScanner(), so the UMD build — which
    defines window.QrScanner synchronously — is the one that must be
    referenced."""
    resp = await paired_client.get("/kiosk")
    assert "/static/vendor/qr-scanner/qr-scanner.umd.min.js" in resp.text
    assert 'type="module"' not in resp.text


async def test_qr_scanner_loads_before_the_board_controller(paired_client):
    resp = await paired_client.get("/kiosk")
    assert resp.text.index("qr-scanner.umd.min.js") < resp.text.index("kiosk-boards.js")


async def test_kiosk_carries_the_camera_hud(paired_client):
    resp = await paired_client.get("/kiosk")
    assert 'id="camera-hud"' in resp.text
    assert 'id="camera-video"' in resp.text


async def test_camera_hud_lives_outside_the_header(paired_client):
    """.kiosk-panel is sized `calc(100vh - 130px)` against a hardcoded header
    height, so anything added to the header resizes every panel — which is why
    the countdown ring is absolutely positioned. The HUD stays out of the
    header entirely and is position:fixed (see test_camera_hud_is_fixed)."""
    resp = await paired_client.get("/kiosk")
    assert resp.text.index("</header>") < resp.text.index('id="camera-hud"')


def test_camera_hud_overrides_the_hidden_attribute():
    """#camera-hud sets an author-level `display`, which beats the UA
    stylesheet's `[hidden] { display: none }` — without this rule
    `hud.hidden = true` silently does nothing. Same trap as .board[hidden] and
    .swap-ring[hidden]."""
    css = KIOSK_CSS.read_text()
    assert "#camera-hud[hidden] { display: none !important; }" in css


def test_camera_hud_is_fixed_position_not_in_flow():
    css = KIOSK_CSS.read_text()
    start = css.index("#camera-hud {")
    hud_rule = css[start:css.index("}", start)]
    assert "position: fixed" in hud_rule


# ── The shared submit path ───────────────────────────────────────────────────

def test_the_two_scanners_share_one_signin_post():
    """The camera path must reuse the wedge scanner's fetch + toast rather than
    copying them, or a change to the sign-in contract would only land on one of
    them."""
    assert BOARDS_JS.read_text().count("'/kiosk/signin'") == 1


def test_scan_debounce_is_a_hardcoded_constant():
    """Deliberately not a pydantic setting: it's browser timing tied to the
    camera's frame rate, not something to tune from an admin form."""
    assert "const SCAN_DEBOUNCE_MS" in BOARDS_JS.read_text()


def test_camera_scanner_is_exported():
    js = BOARDS_JS.read_text()
    assert "initCameraScanner" in js[js.rindex("return {"):]


# ── Degradation ───────────────────────────────────────────────────────────────

async def test_demo_never_starts_a_camera(client):
    """/kiosk/demo is ungated — anyone on the internet can load it — so it must
    never ask for camera permission. It shares kiosk.html with /kiosk/student,
    which carries no camera markup at all; the guarantee is structural, not a
    runtime check."""
    resp = await client.get("/kiosk/demo")
    assert resp.status_code == 200
    assert "qr-scanner" not in resp.text
    assert "camera-hud" not in resp.text
    assert "initCameraScanner" not in resp.text


@pytest.mark.parametrize("path", ["/kiosk/student", "/kiosk/mentor"])
async def test_pinned_boards_have_no_camera(paired_client, path):
    """The pinned views are for a second screen or laptop debugging; the shop
    kiosk is /kiosk. A camera auto-starting on a debugging laptop would be a
    surprise."""
    resp = await paired_client.get(path)
    assert "camera-hud" not in resp.text
    assert "initCameraScanner" not in resp.text


async def test_wedge_scanner_survives_alongside_the_camera(paired_client):
    """The handheld scanner is the fallback for a dead webcam, a flat phone
    battery, or a printed badge — both input paths coexist."""
    resp = await paired_client.get("/kiosk")
    assert 'id="badge-input"' in resp.text
    assert "Kiosk.initBadgeScanner(" in resp.text
