"""
Tests for the neighbor-gap text shown in the /hours Slack slash command's
rank line: how many hours separate a student from the spot above and below
them in the standings (overall and on their team).
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models import AttendanceSession, Team
from app.routers.slack import _neighbor_gap_text


def _row(sid, total, team_id=1):
    return SimpleNamespace(sid=sid, team_id=team_id, total=total)


# ── Unit tests for the helper ──────────────────────────────────────────────

def test_clear_gap_both_sides():
    rows = [_row(1, 10.0), _row(2, 5.0), _row(3, 2.0)]
    assert _neighbor_gap_text(rows, 2, 5.0) == (
        "5.0 hrs behind the spot above · 3.0 hrs ahead of the spot below"
    )


def test_tied_with_spot_below_only():
    # sid 1 and 2 are tied for the lead; ties broken by sid, so 1 sorts first.
    rows = [_row(1, 5.0), _row(2, 5.0), _row(3, 2.0)]
    assert _neighbor_gap_text(rows, 1, 5.0) == "tied with the spot below"


def test_top_place_has_no_above_half():
    rows = [_row(1, 10.0), _row(2, 5.0)]
    assert _neighbor_gap_text(rows, 1, 10.0) == "5.0 hrs ahead of the spot below"


def test_last_place_has_no_below_half():
    rows = [_row(1, 10.0), _row(2, 5.0)]
    assert _neighbor_gap_text(rows, 2, 5.0) == "5.0 hrs behind the spot above"


def test_sole_entry_is_empty():
    rows = [_row(1, 10.0)]
    assert _neighbor_gap_text(rows, 1, 10.0) == ""


# ── Integration test through the /hours endpoint ───────────────────────────

async def _add_session(db, student_id, hours):
    now = datetime.utcnow()
    db.add(AttendanceSession(
        student_id=student_id,
        sign_in_time=now - timedelta(hours=hours),
        sign_out_time=now,
        hours_counted=hours,
    ))
    await db.commit()


async def test_hours_command_shows_overall_and_team_gaps(client, db, team, make_student, monkeypatch):
    import app.routers.slack as slack_module

    async def _skip_verification(request):
        return b""

    monkeypatch.setattr(slack_module, "_verify_slack_signature", _skip_verification)

    team2 = Team(number=4423, name="Team 4423")
    db.add(team2)
    await db.commit()
    await db.refresh(team2)

    # Totals: D=20 (4423, alone on its team), A=10, B=5, C=5 (tie) on 4143.
    a = await make_student(name="A", code="aaaa0001", team_id=team.id)
    b = await make_student(name="B", code="bbbb0001", team_id=team.id)
    c = await make_student(name="C", code="cccc0001", team_id=team.id)
    d = await make_student(name="D", code="dddd0001", team_id=team2.id)
    a.slack_user_id, b.slack_user_id, c.slack_user_id, d.slack_user_id = "UA", "UB", "UC", "UD"
    await db.commit()

    await _add_session(db, a.id, 10.0)
    await _add_session(db, b.id, 5.0)
    await _add_session(db, c.id, 5.0)
    await _add_session(db, d.id, 20.0)

    # B: overall — 5.0 hrs behind A, tied with C. Team — same neighbors.
    resp = await client.post(
        "/slack/command", data={"command": "/hours", "text": "", "user_id": "UB"}
    )
    assert resp.status_code == 200
    assert "_Overall: 5.0 hrs behind the spot above · tied with the spot below_" in resp.text
    assert "_Team: 5.0 hrs behind the spot above · tied with the spot below_" in resp.text

    # D: alone on its team (no Team: line) but trails no one overall (top spot).
    resp = await client.post(
        "/slack/command", data={"command": "/hours", "text": "", "user_id": "UD"}
    )
    assert resp.status_code == 200
    assert "_Overall: 10.0 hrs ahead of the spot below_" in resp.text
    assert "_Team:" not in resp.text
