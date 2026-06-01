"""
tests/test_athlete_profile.py — Unit tests for the AthleteProfile data layer.

Tests cover _row_to_athlete_profile(), the pure helper in supabase.py that maps
a raw DB row dict to an AthleteProfile model. All tests are synchronous and
require no DB connection or secrets — consistent with the existing test suite.

Run with:  pytest tests/test_athlete_profile.py -v
"""

from app.models.schemas import AthleteProfile
from app.services.supabase import _row_to_athlete_profile


# ---------------------------------------------------------------------------
# _row_to_athlete_profile
# ---------------------------------------------------------------------------

def test_none_row_returns_all_defaults():
    """No DB row → all field defaults (ftp_watts=293, weight_kg=74.0)."""
    profile = _row_to_athlete_profile(None)
    assert profile == AthleteProfile()
    assert profile.ftp_watts == 293
    assert profile.weight_kg == 74.0


def test_full_row_populates_both_fields():
    """A row with both fields non-NULL → all values from the row."""
    row = {"ftp_watts": 310, "weight_kg": 73.5}
    profile = _row_to_athlete_profile(row)
    assert profile.ftp_watts == 310
    assert profile.weight_kg == 73.5


def test_partial_row_null_weight_uses_default():
    """ftp_watts present, weight_kg NULL → ftp from row, weight from default."""
    row = {"ftp_watts": 310, "weight_kg": None}
    profile = _row_to_athlete_profile(row)
    assert profile.ftp_watts == 310
    assert profile.weight_kg == 74.0


def test_partial_row_null_ftp_uses_default():
    """weight_kg present, ftp_watts NULL → weight from row, ftp from default."""
    row = {"ftp_watts": None, "weight_kg": 73.5}
    profile = _row_to_athlete_profile(row)
    assert profile.ftp_watts == 293
    assert profile.weight_kg == 73.5
