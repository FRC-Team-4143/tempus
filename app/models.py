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


class FocusCategory(str, enum.Enum):
    software = "software"
    design = "design"
    business = "business"


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


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_code: Mapped[Optional[str]] = mapped_column(String(8), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    team_id: Mapped[int] = mapped_column(Integer, ForeignKey("teams.id"), nullable=False)
    category: Mapped[Optional[FocusCategory]] = mapped_column(
        SAEnum(FocusCategory), nullable=True
    )
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
    mentor_code: Mapped[Optional[str]] = mapped_column(String(8), unique=True, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("teams.id"), nullable=True)
    category: Mapped[Optional[FocusCategory]] = mapped_column(
        SAEnum(FocusCategory), nullable=True
    )
    slack_user_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    is_lead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    team: Mapped[Optional["Team"]] = relationship("Team")
    sessions: Mapped[List["MentorSession"]] = relationship("MentorSession", back_populates="mentor")


class WeeklyRequirement(Base):
    __tablename__ = "weekly_requirements"
    __table_args__ = (UniqueConstraint("team_id", "category", "week_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NULL team_id means the requirement applies to all teams
    team_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("teams.id"), nullable=True)
    category: Mapped[Optional[FocusCategory]] = mapped_column(
        SAEnum(FocusCategory), nullable=True
    )
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
    # Slack message ts for the checkout request; used to update the message after action
    slack_message_ts: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    slack_channel_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    student: Mapped["Student"] = relationship("Student", back_populates="sessions")


class AuditLog(Base):
    """Append-only record of admin mutations (edits, deletes, settings changes).

    There is a single admin login today, so `actor` is "admin"; `ip` records where
    the change came from. `detail` holds an optional JSON blob (e.g. before/after).
    """
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # naive UTC
    actor: Mapped[str] = mapped_column(String(50), nullable=False, default="admin")
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
