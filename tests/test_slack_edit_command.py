"""/edit — a mentor edits a student's session contribution level via a Slack modal.

Replaced a two-step button wizard (pick session -> pick status, each a full round trip
editing the same ephemeral message) with a single modal: `/edit <name>` opens it
directly using the slash command's own `trigger_id`, and both fields are submitted
together. No test coverage existed for /edit before this change.
"""
from datetime import datetime, timedelta

import app.routers.slack as slack_router
from app.models import AttendanceSession, Mentor, SessionStatus


async def _no_signature_check(request):
    return b""


def _bypass_signature(monkeypatch):
    monkeypatch.setattr(slack_router, "_verify_slack_signature", _no_signature_check)


def _capture_open_modal(monkeypatch):
    captured = {}

    async def _fake(trigger_id, view):
        captured["trigger_id"] = trigger_id
        captured["view"] = view
        return True

    monkeypatch.setattr(slack_router, "open_modal", _fake)
    return captured


async def _add_mentor(db, slack_id, name="Coach Ray", code="mnt00001"):
    m = Mentor(name=name, slack_user_id=slack_id, member_code=code, is_active=True)
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def _add_closed_session(
    db, student_id, *, hours_ago=3, length_hours=2, status=SessionStatus.contributor
):
    sign_in = datetime.utcnow() - timedelta(hours=hours_ago)
    s = AttendanceSession(
        student_id=student_id,
        sign_in_time=sign_in,
        sign_out_time=sign_in + timedelta(hours=length_hours),
        status=status,
        hours_counted=length_hours,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def _edit(client, text, user_id="UMENTOR", trigger_id="trig123"):
    return await client.post(
        "/slack/command",
        data={"command": "/edit", "text": text, "user_id": user_id, "trigger_id": trigger_id},
    )


def _submission(view: dict, session_id: int, status: str, user_id: str = "UMENTOR") -> dict:
    return {
        "type": "view_submission",
        "user": {"id": user_id},
        "view": {
            "callback_id": view["callback_id"],
            "state": {
                "values": {
                    "session": {"value": {"selected_option": {"value": str(session_id)}}},
                    "status": {"value": {"selected_option": {"value": status}}},
                },
            },
        },
    }


async def _interact(client, payload: dict):
    import json
    from urllib.parse import urlencode

    body = urlencode({"payload": json.dumps(payload)})
    return await client.post(
        "/slack/interact", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )


# ── /edit command: opening the modal ────────────────────────────────────────────

async def test_edit_requires_a_name(client, monkeypatch):
    _bypass_signature(monkeypatch)
    resp = await _edit(client, "")
    assert "Usage" in resp.text


async def test_edit_rejects_a_non_mentor(client, monkeypatch):
    _bypass_signature(monkeypatch)
    resp = await _edit(client, "Ada", user_id="UNOTAMENTOR")
    assert "Only registered mentors" in resp.text


async def test_edit_no_matching_student(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    resp = await _edit(client, "Nobody Here")
    assert "No student found" in resp.text


async def test_edit_ambiguous_match_lists_names(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    await make_student(name="Ada Lovelace", code="ada00001")
    await make_student(name="Ada Byron", code="ada00002")

    resp = await _edit(client, "Ada")

    assert "Multiple students match" in resp.text
    assert "Ada Lovelace" in resp.text and "Ada Byron" in resp.text


async def test_edit_exact_name_breaks_a_tie(client, db, make_student, monkeypatch):
    """An exact (case-insensitive) match among several partial matches resolves
    without asking the mentor to retype anything more specific."""
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    student = await make_student(name="Ada", code="ada00001")
    await make_student(name="Ada Byron", code="ada00002")
    captured = _capture_open_modal(monkeypatch)
    await _add_closed_session(db, student.id)

    resp = await _edit(client, "ada")

    assert resp.status_code == 200
    assert captured["view"]["title"]["text"] == "Edit — Ada"


async def test_edit_no_past_sessions(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    await make_student(name="Ada Lovelace", code="ada00001")

    resp = await _edit(client, "Ada")

    assert "No past sessions found" in resp.text


async def test_edit_opens_a_modal_with_session_and_status_options(
    client, db, make_student, monkeypatch
):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    student = await make_student(name="Ada Lovelace", code="ada00001")
    session = await _add_closed_session(db, student.id, status=SessionStatus.present)
    captured = _capture_open_modal(monkeypatch)

    resp = await _edit(client, "Ada", trigger_id="trig999")

    assert resp.status_code == 200
    assert captured["trigger_id"] == "trig999"
    view = captured["view"]
    assert view["type"] == "modal"
    assert view["callback_id"] == "edit_session"
    session_opts = view["blocks"][0]["element"]["options"]
    assert [o["value"] for o in session_opts] == [str(session.id)]
    assert "Present" in session_opts[0]["text"]["text"]
    status_opts = view["blocks"][1]["element"]["options"]
    assert [o["value"] for o in status_opts] == ["contributor", "present", "distraction"]


async def test_edit_lists_at_most_five_most_recent_sessions(
    client, db, make_student, monkeypatch
):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    student = await make_student(name="Ada Lovelace", code="ada00001")
    for hours_ago in (10, 20, 30, 40, 50, 60):
        await _add_closed_session(db, student.id, hours_ago=hours_ago)
    captured = _capture_open_modal(monkeypatch)

    await _edit(client, "Ada")

    assert len(captured["view"]["blocks"][0]["element"]["options"]) == 5


async def test_edit_without_trigger_id_fails_gracefully(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    student = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_closed_session(db, student.id)

    resp = await _edit(client, "Ada", trigger_id="")

    assert "Couldn't open the edit form" in resp.text


async def test_edit_reports_when_modal_fails_to_open(client, db, make_student, monkeypatch):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    student = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_closed_session(db, student.id)

    async def _fail(trigger_id, view):
        return False

    monkeypatch.setattr(slack_router, "open_modal", _fail)

    resp = await _edit(client, "Ada")

    assert "Couldn't open the edit form" in resp.text


# ── Modal submission ─────────────────────────────────────────────────────────────

async def test_submitting_the_edit_modal_updates_the_session(
    client, db, make_student, monkeypatch
):
    _bypass_signature(monkeypatch)
    mentor = await _add_mentor(db, "UMENTOR", name="Coach Ray")
    student = await make_student(name="Ada Lovelace", code="ada00001")
    session = await _add_closed_session(
        db, student.id, length_hours=2, status=SessionStatus.contributor
    )
    view = {"callback_id": "edit_session"}

    resp = await _interact(
        client, _submission(view, session.id, "present")
    )

    assert resp.status_code == 200
    await db.refresh(session)
    assert session.status == SessionStatus.present
    assert session.hours_counted == 1.0  # 2h * 50%


async def test_submitting_the_edit_modal_notifies_the_student(
    client, db, make_student, monkeypatch
):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    student = await make_student(name="Ada Lovelace", code="ada00001")
    student.slack_user_id = "USTU"
    await db.commit()
    session = await _add_closed_session(db, student.id)

    notified = []

    async def _fake_notify(student_slack_id, mentor_slack_id, date_str, status_label, hours):
        notified.append((student_slack_id, status_label, hours))

    monkeypatch.setattr(slack_router, "_notify_student_of_status_change", _fake_notify)

    resp = await _interact(client, _submission({"callback_id": "edit_session"}, session.id, "distraction"))

    assert resp.status_code == 200
    assert notified == [("USTU", "Distraction", 0.0)]


async def test_submitting_with_no_session_selected_errors(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")

    payload = {
        "type": "view_submission",
        "user": {"id": "UMENTOR"},
        "view": {
            "callback_id": "edit_session",
            "state": {"values": {"session": {"value": {}}, "status": {"value": {}}}},
        },
    }
    resp = await _interact(client, payload)

    assert resp.json()["response_action"] == "errors"
    assert "session" in resp.json()["errors"]


async def test_submitting_for_a_session_that_was_deleted_errors(client, db, monkeypatch):
    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")

    resp = await _interact(
        client, _submission({"callback_id": "edit_session"}, 999999, "present")
    )

    assert resp.json()["response_action"] == "errors"
    assert "no longer available" in resp.json()["errors"]["session"]


async def test_submitting_reverifies_the_caller_is_still_an_active_mentor(
    client, db, make_student, monkeypatch
):
    """The mentor deactivates (or was never one) between opening the modal and
    submitting it — the old button flow never re-checked this, the modal does."""
    _bypass_signature(monkeypatch)
    mentor = await _add_mentor(db, "UMENTOR")
    mentor.is_active = False
    await db.commit()
    student = await make_student(name="Ada Lovelace", code="ada00001")
    session = await _add_closed_session(db, student.id)

    resp = await _interact(
        client, _submission({"callback_id": "edit_session"}, session.id, "present")
    )

    assert resp.json()["response_action"] == "errors"
    await db.refresh(session)
    assert session.status == SessionStatus.contributor  # unchanged


async def test_unrelated_view_submission_is_a_no_op(client, monkeypatch):
    _bypass_signature(monkeypatch)
    payload = {
        "type": "view_submission",
        "user": {"id": "UMENTOR"},
        "view": {"callback_id": "something_else", "state": {"values": {}}},
    }
    resp = await _interact(client, payload)
    assert resp.status_code == 200
    assert resp.text == ""


async def test_modal_status_labels_reflect_configured_multipliers(
    client, db, make_student, monkeypatch
):
    """The percentages shown must track Admin -> Settings live, not a hardcoded 50%/0%
    — a mentor picking "Present" needs to see what it will actually compute to."""
    from app.config import settings

    _bypass_signature(monkeypatch)
    await _add_mentor(db, "UMENTOR")
    student = await make_student(name="Ada Lovelace", code="ada00001")
    await _add_closed_session(db, student.id)
    captured = _capture_open_modal(monkeypatch)

    original = (settings.contributor_multiplier, settings.present_multiplier, settings.distraction_multiplier)
    settings.contributor_multiplier = 0.9
    settings.present_multiplier = 0.75
    settings.distraction_multiplier = 0.1
    try:
        await _edit(client, "Ada")
    finally:
        (
            settings.contributor_multiplier,
            settings.present_multiplier,
            settings.distraction_multiplier,
        ) = original

    labels = [o["text"]["text"] for o in captured["view"]["blocks"][1]["element"]["options"]]
    assert labels == [
        "✅ Contributor (90% hours)",
        "🔸 Present (75% hours)",
        "🚫 Distraction (10% hours)",
    ]
