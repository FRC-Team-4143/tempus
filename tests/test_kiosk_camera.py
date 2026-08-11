"""The kiosk webcam QR scanner (/kiosk).

A camera on the kiosk PC watches for QR badges on students' phone screens and
feeds the same `POST /kiosk/signin` pipeline as the handheld wedge scanner,
which keeps working as a fallback for printed badges and a dead webcam. The
scanner itself always runs on the combined display. It lives in its own
column at the right of the board (see _board_student.html) rather than
floating over a roster panel — an earlier version overlaid it position:fixed
in the bottom-left corner and had to permanently shorten Team 4143's panel
to keep it clear; a real grid column means nothing else ever needs to make
room for it. What's kiosk-instance-local is only the *preview* (the visible
video box within that column), toggled by a button in the panel's own header
and remembered in that browser's own localStorage (see
Kiosk.initCameraPreviewToggle), not a server setting. /kiosk/demo — which
never runs a real camera — shows a non-functional outline in the same spot
so it previews the real layout. There is no JS test tooling in this project
(no package.json, no browser runner), so the actual scan/decode/debounce/
toggle behaviour is verified by hand — see the kiosk-camera checklist in the
plan this landed from. What's asserted here is everything the *server*
controls or ships: that the library is vendored rather than pulled from a
CDN, that it's the UMD build loaded as a classic script (the ES-module build
would be deferred and evaluate after the inline controller that calls into
it), that the worker sits where the library's relative dynamic import will
look for it, that the two scanners share one submit path rather than
duplicating it, that the preview-toggle button and its JS ship correctly,
that the demo outline never loads real camera code, and that the pinned
boards never carry a camera at all.
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
    """The HUD is a board column, not header chrome — it stays out of
    <header> entirely, same as every other panel."""
    resp = await paired_client.get("/kiosk")
    assert resp.text.index("</header>") < resp.text.index('id="camera-hud"')


def test_camera_hud_overrides_the_hidden_attribute():
    """#camera-hud sets an author-level `display`, which beats the UA
    stylesheet's `[hidden] { display: none }` — without this rule
    `hud.hidden = true` silently does nothing. Same trap as .board[hidden] and
    .swap-ring[hidden]."""
    css = KIOSK_CSS.read_text()
    assert "#camera-hud[hidden] { display: none !important; }" in css


def test_camera_hud_is_a_stacked_panel_not_an_overlay():
    """Regression guard for two earlier designs: the HUD used to be
    position:fixed, floating over whatever roster panel happened to be
    underneath it; a later version gave it a 4th column of its own. It
    shares the Leaderboard's column now (.board-col-stack, see
    _board_student.html), so nothing else ever needs to shrink or narrow
    to stay clear of it."""
    css = KIOSK_CSS.read_text()
    start = css.index(".camera-panel {")
    rule = css[start:css.index("}", start)]
    assert "position: fixed" not in rule
    assert "width:" not in rule       # no fixed column width — full stack width
    # Nothing left over from either earlier approach.
    assert "kiosk-panel-reserve-camera" not in css
    assert "col-auto" not in Path("app/templates/_board_student.html").read_text()


def test_camera_hud_shares_the_leaderboards_column():
    """.board-col-stack wraps Leaderboard and #camera-hud together — verifies
    the structural fact test_camera_hud_is_the_last_board_column below
    depends on."""
    html = Path("app/templates/_board_student.html").read_text()
    stack_start = html.index("board-col-stack")
    stack_body = html[stack_start:html.index("{% endif %}\n      </div>", stack_start)]
    assert "Leaderboard" in stack_body
    assert 'id="camera-hud"' in stack_body


async def test_camera_hud_is_the_last_board_column(paired_client):
    """Sits below the Leaderboard panel, not the old bottom-left corner —
    this is the ordering _board_student.html's markup relies on."""
    resp = await paired_client.get("/kiosk")
    assert resp.text.index("Leaderboard") < resp.text.index('id="camera-hud"')


# ── Following a mentor scan onto the mentor board ───────────────────────────
# The combined display holds on the mentor board for a while after a mentor
# badge scan (kiosk_combined.html's holdMentorBoard), and #camera-hud is one
# live element — one video stream, one qr-scanner instance — not something
# that can be duplicated onto both boards. What's asserted here is the
# structural setup show()'s relocation logic depends on; the actual DOM move
# during a live swap was verified by hand against a real (fake-video-backed)
# browser, same as the rest of this file's JS behaviour.

def test_mentor_board_carries_a_camera_slot():
    """_board_mentor.html needs a landing spot for #camera-hud to be moved
    into (see kiosk_combined.html's show()) — mirrors _board_student.html's
    own slot, which is where the camera actually starts out. Wraps
    Leaderboard, not Mentors — same column-sharing pattern as the student
    board's .board-col-stack."""
    html = Path("app/templates/_board_mentor.html").read_text()
    tag = '<div class="board-col-stack" data-camera-slot>'
    assert tag in html
    # .index(tag), not a bare "data-camera-slot" search — the file's own
    # doc-comment near the top mentions that string too.
    slot_start = html.index(tag)
    assert "Leaderboard" in html[slot_start:]
    assert "Mentors" not in html[slot_start:]


async def test_mentor_boards_leaderboard_gets_the_2x2_grid_too(paired_client):
    """Its Leaderboard shares a column with the camera slot exactly like the
    student board's does, so it needs the same 2x2 .stats-grid treatment —
    otherwise it'd need to scroll the moment #camera-hud actually lands
    there after a mentor scan."""
    resp = await paired_client.get("/kiosk")
    assert resp.text.count('class="scroll-list stats-grid"') == 2  # student board + mentor board


async def test_combined_display_relocates_camera_hud_on_swap(paired_client):
    """show() must physically move #camera-hud into whichever board's
    [data-camera-slot] is coming into view — an appendChild, not a copy — or
    the scan panel would go missing on the mentor board during its hold
    window instead of following the swap."""
    resp = await paired_client.get("/kiosk")
    assert "inEl.querySelector('[data-camera-slot]')" in resp.text
    assert "slot.appendChild(hud)" in resp.text
    # Happens alongside the existing hidden-attribute swap, not the fade —
    # so it's already in place by the time the new board is actually shown.
    move_idx = resp.text.index("slot.appendChild(hud)")
    hidden_idx = resp.text.index("inEl.hidden = false;")
    assert move_idx < hidden_idx


async def test_pinned_mentor_board_slot_never_receives_the_camera(paired_client):
    """/kiosk/mentor has the same [data-camera-slot] markup (harmless — it's
    just an empty flex child) but no show()/relocation script at all, so it
    can never actually receive #camera-hud. Belt-and-suspenders alongside
    test_pinned_boards_have_no_camera, which checks the same page never
    carries the real camera-hud id in the first place."""
    resp = await paired_client.get("/kiosk/mentor")
    assert "data-camera-slot" in resp.text
    assert "appendChild(hud)" not in resp.text


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


# ── Preview show/hide (kiosk-instance-local, not a server setting) ─────────────

async def test_kiosk_carries_the_preview_toggle_button(paired_client):
    resp = await paired_client.get("/kiosk")
    assert 'id="camera-preview-toggle"' in resp.text


async def test_preview_toggle_is_wired_up(paired_client):
    resp = await paired_client.get("/kiosk")
    assert "Kiosk.initCameraPreviewToggle()" in resp.text


def test_preview_toggle_is_exported():
    js = BOARDS_JS.read_text()
    assert "initCameraPreviewToggle" in js[js.rindex("return {"):]


def test_preview_toggle_persists_per_browser_not_a_server_setting():
    """Kiosk-instance-specific means localStorage, not app_settings/.env — each
    physical display remembers its own preference independent of every other
    kiosk in the building, with no admin-panel involvement."""
    js = BOARDS_JS.read_text()
    assert "localStorage" in js
    assert "CAMERA_PREVIEW_HIDDEN_KEY" in js


def test_preview_collapse_never_touches_the_video_or_frames_own_style():
    """Regression guard: qr-scanner inspects .camera-frame/#camera-video's own
    display/visibility/opacity and force-overrides them (discarding its scan
    overlay) if it finds them hidden — see the #camera-hud template comment.
    Both the user toggle (data-preview="hidden") and the broken-camera state
    (data-state="off") must collapse the separate .camera-frame-clip wrapper
    instead, or hiding/losing the preview mid-scan could silently break live
    decoding."""
    css = KIOSK_CSS.read_text()
    clip_start = css.index(".camera-frame-clip {")
    clip_rule = css[clip_start:css.index("}", clip_start)]
    assert "height: 150px" in clip_rule
    collapse_start = css.index('[data-preview="hidden"] .camera-frame-clip')
    collapse_block = css[collapse_start:css.index("}", collapse_start)]
    assert 'data-state="off"] .camera-frame-clip' in collapse_block
    assert "height: 0;" in collapse_block
    # Not applied directly to .camera-frame or #camera-video.
    frame_start = css.index(".camera-frame {")
    frame_rule = css[frame_start:css.index("}", frame_start)]
    assert "display: none" not in frame_rule
    assert "visibility" not in frame_rule
    assert "opacity" not in frame_rule


async def test_demo_never_starts_a_camera(client):
    """/kiosk/demo is ungated — anyone on the internet can load it — so it must
    never ask for camera permission, never load qr-scanner, and never carry
    the real #camera-hud. It shares _board_student.html with the combined
    display, so the guarantee that its {% elif demo %} branch is structurally
    separate from the {% if has_camera_hud %} one matters here, not just a
    runtime check."""
    resp = await client.get("/kiosk/demo")
    assert resp.status_code == 200
    assert "qr-scanner" not in resp.text
    assert "camera-hud" not in resp.text
    assert "initCameraScanner" not in resp.text
    assert "camera-preview-toggle" not in resp.text


async def test_demo_shows_the_camera_panel_outline(client):
    """The demo previews the real /kiosk layout at scale (fake roster, fake
    leaderboard), so it should also preview *where* the camera panel sits —
    without pretending to be one."""
    resp = await client.get("/kiosk/demo")
    assert "camera-panel-outline" in resp.text
    assert "camera-panel-placeholder" in resp.text
    assert "Scan Badge" in resp.text


@pytest.mark.parametrize("path", ["/kiosk/student", "/kiosk/mentor"])
async def test_pinned_boards_have_no_camera(paired_client, path):
    """The pinned views are for a second screen or laptop debugging; the shop
    kiosk is /kiosk. A camera auto-starting on a debugging laptop would be a
    surprise — and unlike /kiosk/demo, these aren't a preview of anything, so
    they get neither the real panel nor its outline."""
    resp = await paired_client.get(path)
    assert "camera-hud" not in resp.text
    assert "initCameraScanner" not in resp.text
    assert "camera-preview-toggle" not in resp.text
    assert "camera-panel-outline" not in resp.text


async def test_wedge_scanner_survives_alongside_the_camera(paired_client):
    """The handheld scanner is the fallback for a dead webcam, a flat phone
    battery, or a printed badge — both input paths coexist."""
    resp = await paired_client.get("/kiosk")
    assert 'id="badge-input"' in resp.text
    assert "Kiosk.initBadgeScanner(" in resp.text
