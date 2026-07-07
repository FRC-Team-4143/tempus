"""Tests for audit logging of admin mutations."""
import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import AttendanceSession, AuditLog, SessionStatus


async def test_session_edit_writes_audit_row(authed_client, db, make_student):
    student = await make_student(code="badge001")
    sign_in = datetime.utcnow() - timedelta(hours=2)
    sess = AttendanceSession(
        student_id=student.id,
        sign_in_time=sign_in,
        sign_out_time=sign_in + timedelta(hours=2),
        status=SessionStatus.contributor,
        hours_counted=2.0,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    # Edit: downgrade to "present" (recalculates to half hours).
    from app.utils import utc_to_local
    resp = await authed_client.post(
        f"/admin/sessions/{sess.id}/edit",
        data={
            "sign_in_time": utc_to_local(sess.sign_in_time).strftime("%Y-%m-%dT%H:%M"),
            "sign_out_time": utc_to_local(sess.sign_out_time).strftime("%Y-%m-%dT%H:%M"),
            "status": "present",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = (await db.execute(
        select(AuditLog).where(AuditLog.action == "session.edit")
    )).scalars().all()
    assert len(rows) == 1
    entry = rows[0]
    assert entry.entity_type == "session"
    assert entry.entity_id == str(sess.id)
    detail = json.loads(entry.detail)
    assert detail["before"]["status"] == "contributor"
    assert detail["after"]["status"] == "present"
    # Actor is the SSO identity from the mw_sso cookie (not a hardcoded "admin").
    assert entry.actor == "test.admin"
