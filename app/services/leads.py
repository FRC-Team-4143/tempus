"""
Lead resolution — which mentors get looped into a student's escalation DM.

"Lead" is no longer a local boolean; it's a Legion user group. A mentor leads a student
when they hold the `tempus-lead-<team_number>-<subteam_slug>` group (synced into
`Mentor.group_slugs` by `legion_sync`). This is the single source of truth for both the
on-demand behind-notification (`slack_client.notify_student_hours`) and the scheduled
weekly DM (`scheduler.job_weekly_dms`), which previously disagreed on lead handling.
"""
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Mentor, Student, Team


async def team_number_for(db: AsyncSession, team_id: Optional[int]) -> Optional[int]:
    if team_id is None:
        return None
    return (await db.execute(select(Team.number).where(Team.id == team_id))).scalar_one_or_none()


async def lead_mentors_for_student(db: AsyncSession, student: Student) -> list[Mentor]:
    """Active mentors (with a Slack id) who hold the student's
    `tempus-lead-<team>-<subteam>` group. Empty when the student has no team/subteam."""
    team_number = await team_number_for(db, student.team_id)
    if team_number is None or not student.subteam_slug:
        return []
    rows = (await db.execute(
        select(Mentor).where(
            Mentor.slack_user_id.is_not(None),
            Mentor.is_active.is_(True),
            Mentor.group_slugs.is_not(None),
        )
    )).scalars().all()
    return [m for m in rows if m.leads(team_number, student.subteam_slug)]
