"""Tests for the unauthenticated, predetermined badge page at /badge/<id>: no Legion
SSO session required, and an unmatched id must be indistinguishable from a real one."""
import io

from PIL import Image

from app.services.badge import compute_badge_id


def test_compute_badge_id_is_deterministic():
    assert compute_badge_id("ada00001") == compute_badge_id("ada00001")


def test_compute_badge_id_differs_per_code():
    assert compute_badge_id("ada00001") != compute_badge_id("mnt00001")


async def test_badge_page_returns_200_for_real_and_garbage_ids(client, db, make_student):
    await make_student(code="ada00001")
    real_id = compute_badge_id("ada00001")

    real_resp = await client.get(f"/badge/{real_id}")
    garbage_resp = await client.get("/badge/not-a-real-id-at-all")

    assert real_resp.status_code == 200
    assert garbage_resp.status_code == 200


async def test_badge_page_html_is_identical_shape_for_real_and_garbage_ids(client, db, make_student):
    await make_student(code="ada00001")
    real_id = compute_badge_id("ada00001")
    garbage_id = "not-a-real-id-at-all"

    real_resp = await client.get(f"/badge/{real_id}")
    garbage_resp = await client.get(f"/badge/{garbage_id}")

    real_html = real_resp.text.replace(real_id, "X")
    garbage_html = garbage_resp.text.replace(garbage_id, "X")
    assert real_html == garbage_html
    assert "Ada Lovelace" not in real_resp.text


async def test_badge_png_returns_image_for_real_and_garbage_ids(client, db, make_student):
    await make_student(code="ada00001")
    real_id = compute_badge_id("ada00001")

    real_resp = await client.get(f"/badge/{real_id}.png")
    garbage_resp = await client.get("/badge/not-a-real-id-at-all.png")

    assert real_resp.status_code == 200
    assert real_resp.headers["content-type"] == "image/png"
    assert garbage_resp.status_code == 200
    assert garbage_resp.headers["content-type"] == "image/png"


async def test_badge_png_is_identical_size_for_real_and_garbage_ids(client, db, make_student):
    """The image dimensions are as much a part of the "shape" as the HTML/status code
    — a QR that auto-sizes to its payload would let the *smaller* real-code PNG be
    told apart from the longer badge_id-fallback PNG on a miss, even though the page
    and status code around it look identical."""
    await make_student(code="ada00001")
    real_id = compute_badge_id("ada00001")

    real_resp = await client.get(f"/badge/{real_id}.png")
    garbage_resp = await client.get("/badge/not-a-real-id-at-all.png")

    real_size = Image.open(io.BytesIO(real_resp.content)).size
    garbage_size = Image.open(io.BytesIO(garbage_resp.content)).size
    assert real_size == garbage_size


async def test_badge_page_works_with_no_cookies_at_all(client, db, make_student):
    """The whole point: viewing the badge never requires a Legion SSO session."""
    await make_student(code="ada00001")
    real_id = compute_badge_id("ada00001")

    resp = await client.get(f"/badge/{real_id}")
    assert resp.status_code == 200
    assert "Sign in" not in resp.text
