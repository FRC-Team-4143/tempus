from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel

from app.models import SessionStatus, FocusCategory


# ── Teams ──────────────────────────────────────────────────────────────────────

class TeamOut(BaseModel):
    id: int
    number: int
    name: str

    model_config = {"from_attributes": True}


# ── Students ───────────────────────────────────────────────────────────────────

class StudentCreate(BaseModel):
    name: str
    team_id: int
    category: Optional[FocusCategory] = None
    slack_user_id: Optional[str] = None


class StudentUpdate(BaseModel):
    name: Optional[str] = None
    team_id: Optional[int] = None
    category: Optional[FocusCategory] = None
    slack_user_id: Optional[str] = None


class StudentOut(BaseModel):
    id: int
    name: str
    team_id: int
    category: Optional[FocusCategory]
    slack_user_id: Optional[str]
    team: TeamOut

    model_config = {"from_attributes": True}


# ── Mentors ────────────────────────────────────────────────────────────────────

class MentorCreate(BaseModel):
    name: str
    slack_user_id: str


class MentorOut(BaseModel):
    id: int
    name: str
    slack_user_id: str

    model_config = {"from_attributes": True}


# ── Weekly Requirements ────────────────────────────────────────────────────────

class WeeklyRequirementCreate(BaseModel):
    team_id: int
    category: Optional[FocusCategory] = None
    week_start: date
    required_hours: float


class WeeklyRequirementOut(BaseModel):
    id: int
    team_id: int
    category: Optional[FocusCategory]
    week_start: date
    required_hours: float
    team: TeamOut

    model_config = {"from_attributes": True}


# ── Sessions ───────────────────────────────────────────────────────────────────

class SessionOut(BaseModel):
    id: int
    student_id: int
    sign_in_time: datetime
    sign_out_time: Optional[datetime]
    status: Optional[SessionStatus]
    hours_counted: Optional[float]
    student: StudentOut

    model_config = {"from_attributes": True}


# ── Kiosk ──────────────────────────────────────────────────────────────────────

class SignInRequest(BaseModel):
    name: str  # Tracker UID (student_code) encoded in the QR badge


class SignInResponse(BaseModel):
    success: bool
    message: str
    student_name: Optional[str] = None
    team_name: Optional[str] = None
