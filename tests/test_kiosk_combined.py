"""The combined auto-swapping display at /kiosk.

The swap itself is browser timing (a mentor scan holds the mentor board, a student
scan snaps back), local to the scanning browser — driven by that browser's own
`/kiosk/signin` response, not the SSE broadcast — and stays out of pytest (server-side
coverage of that response lives in test_kiosk_signin.py). What the *server* controls
is testable and tested here: which template each URL serves, that the hold duration
is rendered from config rather than hardcoded, that the SSE handlers only refresh
data rather than swapping, and that the pinned views don't carry the controller.
"""
import pytest

from app.config import settings


async def test_kiosk_serves_the_combined_display(paired_client):
    resp = await paired_client.get("/kiosk")
    assert resp.status_code == 200
    # Both boards present in one document — the swap is a visibility toggle, not
    # a navigation, so the display never reloads mid-shift.
    assert 'id="student-board"' in resp.text
    assert 'id="mentor-board"' in resp.text
    assert 'id="swap-ring"' in resp.text


async def test_combined_display_subscribes_to_the_student_stream(paired_client):
    """That stream forwards *every* broadcast event including mentor_update, so one
    connection keeps both boards' data current. Subscribing to the mentor stream
    instead would silently lose student updates. (Data only — see
    test_sse_handlers_only_refresh_data_not_swap for why the board *swap* doesn't
    ride this stream.)"""
    resp = await paired_client.get("/kiosk")
    assert "/kiosk/student/stream" in resp.text
    assert "mentor_update" in resp.text
    assert "'/kiosk/mentor/stream'" not in resp.text


async def test_sse_handlers_only_refresh_data_not_swap(paired_client):
    """The board swap is local to the scanning browser (driven by its own
    /kiosk/signin response — see test_both_scanners_share_one_local_result_callback),
    not by this SSE stream. A broadcast with no associated display — the nightly
    auto-sign-out, an admin edit, a Slack /gtfo — must refresh every open kiosk's
    counts without swapping any of them."""
    resp = await paired_client.get("/kiosk")
    start = resp.text.index("connectSSE(")
    end = resp.text.index(");", start)
    handlers_src = resp.text[start:end]
    assert "toStudent()" not in handlers_src
    assert "holdMentorBoard()" not in handlers_src


async def test_both_scanners_share_one_local_result_callback(paired_client, monkeypatch):
    """The combined display swaps itself from its own scan result, and both input
    paths — the handheld wedge scanner and the kiosk webcam — must drive that from
    the *same* callback, or a camera scan could sign a mentor in without swapping
    to the mentor board. The pinned single-board pages have nothing to swap and
    call the initialisers with no argument (see test_kiosk_pages.py). Camera is
    off by default (see test_kiosk_camera.py), so it has to be switched on here
    to see initCameraScanner rendered at all."""
    monkeypatch.setattr(settings, "camera_scanner_enabled", True)
    resp = await paired_client.get("/kiosk")
    assert "Kiosk.initBadgeScanner(onScan)" in resp.text
    assert "Kiosk.initCameraScanner(onScan)" in resp.text


async def test_hold_duration_comes_from_config(paired_client, monkeypatch):
    monkeypatch.setattr(settings, "kiosk_mentor_hold_seconds", 45)
    resp = await paired_client.get("/kiosk")
    assert "const HOLD_MS = 45 * 1000;" in resp.text


async def test_hold_duration_is_never_hardcoded(paired_client):
    """Asserted against the live setting rather than a literal, so a deployment
    that tunes KIOSK_MENTOR_HOLD_SECONDS in .env doesn't fail the suite."""
    resp = await paired_client.get("/kiosk")
    assert f"const HOLD_MS = {settings.kiosk_mentor_hold_seconds} * 1000;" in resp.text


def test_hold_duration_defaults_to_two_minutes():
    """The default belongs to the Settings class, so check it there — reading it
    off a rendered page would just be reading back whatever .env happens to say."""
    from app.config import Settings

    assert Settings.model_fields["kiosk_mentor_hold_seconds"].default == 120


@pytest.mark.parametrize("path,expect_board,reject_board", [
    ("/kiosk/student", 'id="student-board"', 'id="mentor-board"'),
    ("/kiosk/mentor", 'id="mentor-board"', 'id="student-board"'),
])
async def test_pinned_views_carry_one_board_and_no_swap(
    paired_client, path, expect_board, reject_board
):
    resp = await paired_client.get(path)
    assert resp.status_code == 200
    assert expect_board in resp.text
    assert reject_board not in resp.text
    assert "HOLD_MS" not in resp.text
    assert 'id="swap-ring"' not in resp.text


@pytest.mark.parametrize("path", ["/kiosk", "/kiosk/student", "/kiosk/mentor"])
async def test_boards_share_the_extracted_assets(paired_client, path):
    """All three pages render boards, so the CSS and render/refresh logic live in
    /static rather than being copied into each template."""
    resp = await paired_client.get(path)
    assert "/static/kiosk-boards.js" in resp.text
    assert "/static/kiosk.css" in resp.text


async def test_combined_display_has_no_cross_links(paired_client):
    """The hardware has no mouse, so a nav link to the other board would be dead
    weight — the swap is how the mentor board is reached."""
    resp = await paired_client.get("/kiosk")
    assert 'href="/kiosk/mentor"' not in resp.text
    assert 'href="/kiosk/student"' not in resp.text


async def test_demo_still_renders_without_pairing_or_gated_fetches(client):
    """/kiosk/demo is ungated, so it must not call the paired-only data endpoints —
    its stats are inlined by the server instead."""
    resp = await client.get("/kiosk/demo")
    assert resp.status_code == 200
    assert "demoStats:" in resp.text
    assert "/kiosk/student/stream" not in resp.text
