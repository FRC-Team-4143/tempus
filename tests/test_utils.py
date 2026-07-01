"""Tests for app/utils.py time helpers."""
from datetime import datetime

import pytest

from app import utils
from app.utils import effective_signout_utc, local_to_utc


def _patch_now(monkeypatch, naive_local: datetime):
    """Freeze utils.datetime.now() to a given naive local wall-clock time."""
    tz = utils._tz()
    aware = naive_local.replace(tzinfo=tz)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return aware.astimezone(tzinfo) if tzinfo else aware.replace(tzinfo=None)

    monkeypatch.setattr(utils, "datetime", _FakeDatetime)


def test_effective_signout_utc_returns_recent_moment(monkeypatch):
    """22:00 while it is 23:00 local resolves to today's 22:00 (in the past)."""
    _patch_now(monkeypatch, datetime(2026, 7, 1, 23, 0))

    result = effective_signout_utc("22:00")

    assert result == local_to_utc(datetime(2026, 7, 1, 22, 0))


def test_effective_signout_utc_rolls_back_past_midnight(monkeypatch):
    """When the job fires after midnight, 22:00 means the PRIOR day's 22:00."""
    _patch_now(monkeypatch, datetime(2026, 7, 2, 0, 30))  # 00:30, just after midnight

    result = effective_signout_utc("22:00")

    # Rolled back to July 1 22:00, not July 2.
    assert result == local_to_utc(datetime(2026, 7, 1, 22, 0))


@pytest.mark.parametrize("bad", ["", "not-a-time", "25:00", "22:99", "22", None])
def test_effective_signout_utc_malformed_returns_none(bad):
    assert effective_signout_utc(bad) is None
