"""Tests for the active/archived flag on sign-in.

Roster CRUD (create/edit/archive/restore/purge) moved to Legion — Tempus mirrors the
active flag via the sync job (see test_legion_sync.py), so those route tests are gone.
"""
from app.services.attendance import sign_in


async def test_archived_student_cannot_sign_in(db, make_student):
    await make_student(code="badge001", is_active=False)

    ok, msg, returned = await sign_in(db, "badge001")

    assert ok is False
    assert returned is None


async def test_active_student_can_still_sign_in(db, make_student):
    await make_student(code="badge001", is_active=True)

    ok, _, returned = await sign_in(db, "badge001")

    assert ok is True
    assert returned is not None
