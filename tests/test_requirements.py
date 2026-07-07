"""Tests for weekly requirement fallback resolution (app/services/requirements.py)."""
from datetime import date

import pytest

from app.models import WeeklyRequirement
from app.services.requirements import (
    DEFAULT_REQUIRED_HOURS,
    requirement_lookup_order,
    resolve_requirement,
)

MONDAY = date(2026, 6, 15)  # a Monday
SOFTWARE = "software"


def test_lookup_order_is_most_specific_first():
    order = requirement_lookup_order(team_id=1, subteam_slug=SOFTWARE)
    assert order == [
        (1, SOFTWARE),      # team + subteam
        (1, None),          # team, any subteam
        (None, SOFTWARE),   # all teams + subteam
        (None, None),       # all teams, any subteam
    ]


def test_lookup_order_without_subteam():
    order = requirement_lookup_order(team_id=1, subteam_slug=None)
    assert order == [(1, None), (None, None)]


async def test_falls_back_to_default_when_no_rows(db, team):
    req = await resolve_requirement(db, team.id, SOFTWARE, MONDAY)
    assert req == DEFAULT_REQUIRED_HOURS


async def test_all_teams_rule_applies_when_no_team_specific(db, team):
    db.add(WeeklyRequirement(team_id=None, subteam_slug=None, week_start=MONDAY, required_hours=8.0))
    await db.commit()

    req = await resolve_requirement(db, team.id, SOFTWARE, MONDAY)
    assert req == 8.0


async def test_team_specific_beats_all_teams(db, team):
    db.add(WeeklyRequirement(team_id=None, subteam_slug=None, week_start=MONDAY, required_hours=8.0))
    db.add(WeeklyRequirement(team_id=team.id, subteam_slug=None, week_start=MONDAY, required_hours=12.0))
    await db.commit()

    req = await resolve_requirement(db, team.id, SOFTWARE, MONDAY)
    assert req == 12.0


async def test_team_plus_subteam_is_most_specific(db, team):
    db.add(WeeklyRequirement(team_id=team.id, subteam_slug=None, week_start=MONDAY, required_hours=12.0))
    db.add(WeeklyRequirement(
        team_id=team.id, subteam_slug=SOFTWARE, week_start=MONDAY, required_hours=15.0,
    ))
    await db.commit()

    req = await resolve_requirement(db, team.id, SOFTWARE, MONDAY)
    assert req == 15.0


async def test_most_recent_week_at_or_before_target_wins(db, team):
    db.add(WeeklyRequirement(team_id=team.id, subteam_slug=None, week_start=date(2026, 6, 1), required_hours=10.0))
    db.add(WeeklyRequirement(team_id=team.id, subteam_slug=None, week_start=date(2026, 6, 8), required_hours=11.0))
    # A future requirement must NOT apply to the earlier target week.
    db.add(WeeklyRequirement(team_id=team.id, subteam_slug=None, week_start=date(2026, 6, 22), required_hours=20.0))
    await db.commit()

    req = await resolve_requirement(db, team.id, None, MONDAY)  # 2026-06-15
    assert req == 11.0
