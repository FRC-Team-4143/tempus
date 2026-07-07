import enum
from datetime import datetime, date
from typing import Optional, List

from sqlalchemy import (
    Integer, String, Boolean, Float, DateTime, Date, Text,
    ForeignKey, Enum as SAEnum, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SessionStatus(str, enum.Enum):
    contributor = "contributor"
    present = "present"
    auto = "auto"
    distraction = "distraction"


class AppSetting(Base):
    """Small key/value store for runtime-configurable app settings.

    Currently holds the optional "leaderboard_since" cutoff date (ISO string);
    a missing/blank value means leaderboard totals count all-time.
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    students: Mapped[List["Student"]] = relationship("Student", back_populates="team")
    weekly_requirements: Mapped[List["WeeklyRequirement"]] = relationship(
        "WeeklyRequirement", back_populates="team"
    )


class Subteam(Base):
    """Local mirror of Legion's subteams (formerly Tempus's hardcoded FocusCategory enum).

    Synced from Legion's `GET /api/subteams`; `slug` is the stable key stored on
    Student/Mentor/WeeklyRequirement (`subteam_slug`) and used to build the
    `tempus-lead-<team>-<subteam>` group slug. `label` is for display only.
    """
    __tablename__ = "subteams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Legion's canonical id — the QR-badge / kiosk sign-in identity and the roster sync key.
    member_code: Mapped[Optional[str]] = mapped_column(String(16), unique=True, nullable=True, index=True)
    # Legacy sha256(name)[:8] badge code — kept read-only for old badges during the cutover.
    student_code: Mapped[Optional[str]] = mapped_column(String(8), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("teams.id"), nullable=False)
    # Legion subteam slug (e.g. "software"); NULL = unassigned / all-subteams scope.
    subteam_slug: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    slack_user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    team: Mapped["Team"] = relationship("Team", back_populates="students")
    sessions: Mapped[List["AttendanceSession"]] = relationship(
        "AttendanceSession", back_populates="student"
    )


class Mentor(Base):
    __tablename__ = "mentors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_code: Mapped[Optional[str]] = mapped_column(String(16), unique=True, nullable=True, index=True)
    mentor_code: Mapped[Optional[str]] = mapped_column(String(8), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("teams.id"), nullable=True)
    subteam_slug: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    slack_user_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    # JSON list of the member's Legion group slugs (synced). Used to decide lead status:
    # a mentor leads a student when `tempus-lead-<team>-<subteam>` is in this list.
    group_slugs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    team: Mapped[Optional["Team"]] = relationship("Team")
    sessions: Mapped[List["MentorSession"]] = relationship("MentorSession", back_populates="mentor")

    @property
    def groups(self) -> List[str]:
        """The parsed Legion group slugs (empty list when unset)."""
        import json
        if not self.group_slugs:
            return []
        try:
            return list(json.loads(self.group_slugs))
        except (ValueError, TypeError):
            return []

    def leads(self, team_number: Optional[int], subteam_slug: Optional[str]) -> bool:
        """True if this mentor holds the `tempus-lead-<team>-<subteam>` group for the
        given student scope. Requires both a team and a subteam to form the slug."""
        if team_number is None or not subteam_slug:
            return False
        return f"tempus-lead-{team_number}-{subteam_slug}" in self.groups

    @property
    def lead_groups(self) -> List[str]:
        """This mentor's `tempus-lead-*` group slugs, for display (e.g. admin roster)."""
        return [g for g in self.groups if g.startswith("tempus-lead-")]


class WeeklyRequirement(Base):
    __tablename__ = "weekly_requirements"
    __table_args__ = (UniqueConstraint("team_id", "subteam_slug", "week_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NULL team_id means the requirement applies to all teams
    team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("teams.id"), nullable=True)
    # NULL subteam_slug means the requirement applies to all subteams
    subteam_slug: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)  # always a Monday
    required_hours: Mapped[float] = mapped_column(Float, nullable=False, default=11.0)

    team: Mapped[Optional["Team"]] = relationship("Team", back_populates="weekly_requirements")


class AttendanceSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("students.id"), nullable=False
    )
    sign_in_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sign_out_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[Optional[SessionStatus]] = mapped_column(
        SAEnum(SessionStatus), nullable=True
    )
    hours_counted: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Time the /checkout command was run; used as the sign-out time when the button is clicked
    checkout_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    student: Mapped["Student"] = relationship("Student", back_populates="sessions")


class AuditLog(Base):
    """Append-only record of admin mutations (edits, deletes, settings changes).

    `actor` is the signed-in admin's Legion SSO username (or "system" for scheduled
    jobs); `ip` records where the change came from. `detail` holds an optional JSON blob.
    """
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    actor: Mapped[str] = mapped_column(String(50), nullable=False, default="system")
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. "session.edit"
    entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON


class MentorSession(Base):
    """Tracks mentor sign-ins — just for fun, separate from student attendance."""
    __tablename__ = "mentor_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mentor_id: Mapped[int] = mapped_column(Integer, ForeignKey("mentors.id"), nullable=False)
    sign_in_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sign_out_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    hours_counted: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    mentor: Mapped["Mentor"] = relationship("Mentor", back_populates="sessions")
