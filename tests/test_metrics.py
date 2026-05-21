"""
tests/test_metrics.py — Unit tests for app/services/metrics.py.

All functions under test are pure Python (no I/O, no async), so no mocking or
fixtures are needed. Tests are organized in the same order as the functions they
cover in metrics.py.

Run with:  .venv/bin/python -m pytest tests/test_metrics.py -v
"""

import dataclasses
import math

import pytest

from app.services.metrics import (
    ActivityMetrics,
    ClimbSegment,
    activity_metrics_from_dict,
    compute_activity_metrics,
    compute_hr_decoupling,
    compute_normalized_power,
    compute_power_duration_curve,
    compute_time_in_zones,
    compute_variability_index,
    extract_climb_segments,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def constant_stream(value: float, n: int) -> list[float]:
    """Return a list of n identical float values."""
    return [value] * n


# ---------------------------------------------------------------------------
# compute_normalized_power
# ---------------------------------------------------------------------------

def test_np_empty_list_returns_none():
    assert compute_normalized_power([]) is None


def test_np_fewer_than_30_samples_returns_none():
    # 29 is the last count that should return None
    assert compute_normalized_power(constant_stream(200.0, 29)) is None


def test_np_exactly_30_samples_returns_value():
    # 30 is the minimum count that should produce a result
    result = compute_normalized_power(constant_stream(200.0, 30))
    assert result is not None


def test_np_constant_power_equals_average():
    # For a constant stream, every 30s rolling average == that constant,
    # so NP == constant. This gives an exact reference value.
    result = compute_normalized_power(constant_stream(200.0, 60))
    assert result == pytest.approx(200.0, rel=1e-6)


def test_np_variable_power_exceeds_average():
    # 300s at 0W then 300s at 200W → average = 100W.
    # NP must be > 100W because the 4th-power transform penalises variability.
    watts = constant_stream(0.0, 300) + constant_stream(200.0, 300)
    result = compute_normalized_power(watts)
    avg = sum(watts) / len(watts)  # 100.0
    assert result is not None
    assert result > avg


# ---------------------------------------------------------------------------
# compute_variability_index
# ---------------------------------------------------------------------------

def test_vi_zero_avg_power_returns_none():
    assert compute_variability_index(200.0, 0.0) is None


def test_vi_steady_effort_equals_one():
    # When NP == average, VI = 1.0 (perfectly steady)
    assert compute_variability_index(200.0, 200.0) == pytest.approx(1.0)


def test_vi_exact_calculation():
    # 250 / 200 = 1.25
    assert compute_variability_index(250.0, 200.0) == pytest.approx(1.25)


# ---------------------------------------------------------------------------
# compute_time_in_zones
#
# Using FTP=100 so zone boundaries are round numbers:
#   Z1: w <  55   (frac < 0.55)
#   Z2: 55 <= w < 75
#   Z3: 75 <= w < 90
#   Z4: 90 <= w < 105
#   Z5: 105 <= w < 120
#   Z6: w >= 120
# ---------------------------------------------------------------------------

FTP = 100.0


def test_zones_one_sample_per_zone():
    # Each value sits squarely in its zone — one second each.
    watts = [50.0, 65.0, 80.0, 95.0, 110.0, 130.0]
    result = compute_time_in_zones(watts, FTP)
    assert result == {"Z1": 1, "Z2": 1, "Z3": 1, "Z4": 1, "Z5": 1, "Z6": 1}


def test_zones_all_zeros_fall_in_z1():
    # 0W / FTP = 0.0, which is in [0.0, 0.55) → Z1
    result = compute_time_in_zones(constant_stream(0.0, 60), FTP)
    assert result["Z1"] == 60
    assert result["Z2"] == result["Z3"] == result["Z4"] == result["Z5"] == result["Z6"] == 0


def test_zones_boundary_inclusive_lower():
    # 0.55 × FTP = 55W — the lower bound of Z2 is inclusive
    result = compute_time_in_zones([55.0], FTP)
    assert result["Z2"] == 1
    assert result["Z1"] == 0


def test_zones_boundary_exclusive_upper():
    # 54W is just below the Z2 lower bound, so it lands in Z1
    result = compute_time_in_zones([54.0], FTP)
    assert result["Z1"] == 1
    assert result["Z2"] == 0


def test_zones_all_samples_are_counted():
    # The sum of all zone counts must equal the number of samples
    watts = [50.0, 65.0, 80.0, 95.0, 110.0, 130.0] * 10  # 60 samples
    result = compute_time_in_zones(watts, FTP)
    assert sum(result.values()) == len(watts)


def test_zones_ftp_zero_guard():
    # When FTP=0, frac is always 0.0 → all samples land in Z1
    result = compute_time_in_zones([100.0, 200.0, 300.0], 0.0)
    assert result["Z1"] == 3
    assert result["Z2"] == result["Z3"] == result["Z4"] == result["Z5"] == result["Z6"] == 0


# ---------------------------------------------------------------------------
# compute_power_duration_curve
# ---------------------------------------------------------------------------

_ALL_PDC_LABELS = {"5s", "15s", "30s", "1m", "2m", "3m", "5m", "10m", "15m", "20m", "30m", "45m", "60m"}


def test_pdc_returns_all_13_keys():
    result = compute_power_duration_curve(constant_stream(200.0, 3600))
    assert set(result.keys()) == _ALL_PDC_LABELS


def test_pdc_empty_stream_all_none():
    result = compute_power_duration_curve([])
    assert set(result.keys()) == _ALL_PDC_LABELS
    assert all(v is None for v in result.values())


def test_pdc_short_ride_longer_durations_are_none():
    # 30 samples: 5s/15s/30s should exist; 1m and beyond should be None
    result = compute_power_duration_curve(constant_stream(200.0, 30))
    assert result["5s"]  is not None
    assert result["15s"] is not None
    assert result["30s"] is not None
    assert result["1m"]  is None
    assert result["5m"]  is None
    assert result["60m"] is None


def test_pdc_constant_stream_all_durations_equal_value():
    # For a constant stream, best average for any window = that constant.
    result = compute_power_duration_curve(constant_stream(200.0, 3600))
    for label, value in result.items():
        assert value == pytest.approx(200.0, rel=1e-6), f"Expected 200.0 for {label}, got {value}"


# ---------------------------------------------------------------------------
# compute_hr_decoupling
# ---------------------------------------------------------------------------

def test_decoupling_too_short_returns_none():
    # Need at least 60 paired samples
    watts = constant_stream(200.0, 59)
    hr    = constant_stream(130.0, 59)
    assert compute_hr_decoupling(watts, hr) is None


def test_decoupling_constant_power_and_hr_is_zero():
    # If EF is identical in both halves, decoupling = 0%
    watts = constant_stream(200.0, 120)
    hr    = constant_stream(130.0, 120)
    result = compute_hr_decoupling(watts, hr)
    assert result == pytest.approx(0.0, abs=1e-6)


def test_decoupling_positive_when_hr_rises():
    # First half: 200W @ 130bpm → EF₁ = 200/130
    # Second half: 200W @ 150bpm → EF₂ = 200/150
    # Decoupling = (1 - 130/150) × 100 = 20/150 × 100 = 13.333...%
    watts = constant_stream(200.0, 60)
    hr    = constant_stream(130.0, 30) + constant_stream(150.0, 30)
    result = compute_hr_decoupling(watts, hr)
    expected = (1.0 - 130.0 / 150.0) * 100.0
    assert result == pytest.approx(expected, rel=1e-6)


def test_decoupling_negative_when_hr_drops():
    # HR falls relative to power in second half → negative decoupling
    watts = constant_stream(200.0, 60)
    hr    = constant_stream(150.0, 30) + constant_stream(130.0, 30)
    result = compute_hr_decoupling(watts, hr)
    assert result is not None
    assert result < 0.0


def test_decoupling_zero_samples_excluded():
    # Second half is all zeros → fewer than 10 valid pairs → returns None
    watts = constant_stream(200.0, 30) + constant_stream(0.0, 30)
    hr    = constant_stream(130.0, 30) + constant_stream(0.0, 30)
    assert compute_hr_decoupling(watts, hr) is None


def test_decoupling_uses_shorter_stream_length():
    # If watts and heartrate differ in length, min(len) is used — should not raise.
    watts = constant_stream(200.0, 120)
    hr    = constant_stream(130.0, 80)  # shorter
    result = compute_hr_decoupling(watts, hr)
    assert result is not None  # 80 samples, both halves valid


# ---------------------------------------------------------------------------
# extract_climb_segments
# ---------------------------------------------------------------------------

def _flat(n: int) -> list[float]:
    return constant_stream(1.0, n)  # well below the 4% default threshold


def _climb(n: int, grade: float = 5.0) -> list[float]:
    return constant_stream(grade, n)


def test_climbs_empty_grade_stream():
    assert extract_climb_segments([], [], []) == []


def test_climbs_all_flat_no_segments():
    assert extract_climb_segments(
        constant_stream(200.0, 120),
        constant_stream(140.0, 120),
        _flat(120),
    ) == []


def test_climbs_too_short_filtered_out():
    # 59s of climbing — just below the 60s default minimum
    grade = _flat(10) + _climb(59) + _flat(10)
    watts = constant_stream(250.0, len(grade))
    hr    = constant_stream(160.0, len(grade))
    assert extract_climb_segments(watts, hr, grade) == []


def test_climbs_exactly_min_duration_included():
    # 60s is the boundary — should produce exactly one segment
    grade = _flat(10) + _climb(60) + _flat(10)
    watts = constant_stream(250.0, len(grade))
    hr    = constant_stream(160.0, len(grade))
    result = extract_climb_segments(watts, hr, grade)
    assert len(result) == 1
    assert result[0].duration_seconds == 60


def test_climbs_single_segment_all_fields():
    # Reference climb: 10 flat + 100s @ 5% + 10 flat
    grade = _flat(10) + _climb(100, grade=5.0) + _flat(10)
    watts = constant_stream(250.0, len(grade))
    hr    = constant_stream(160.0, len(grade))
    result = extract_climb_segments(watts, hr, grade)
    assert len(result) == 1
    seg = result[0]
    assert seg.duration_seconds  == 100
    assert seg.avg_power_watts   == pytest.approx(250.0)
    assert seg.avg_hr_bpm        == pytest.approx(160.0)
    assert seg.avg_grade_pct     == pytest.approx(5.0)


def test_climbs_two_segments_in_order():
    grade = _flat(10) + _climb(80) + _flat(20) + _climb(90) + _flat(10)
    watts = constant_stream(250.0, len(grade))
    hr    = constant_stream(160.0, len(grade))
    result = extract_climb_segments(watts, hr, grade)
    assert len(result) == 2
    assert result[0].duration_seconds == 80
    assert result[1].duration_seconds == 90


def test_climbs_ride_ends_mid_climb():
    # The stream ends while still on a climb — the final segment must be captured.
    grade = _flat(10) + _climb(100)  # no trailing flat section
    watts = constant_stream(250.0, len(grade))
    hr    = constant_stream(160.0, len(grade))
    result = extract_climb_segments(watts, hr, grade)
    assert len(result) == 1
    assert result[0].duration_seconds == 100


def test_climbs_no_power_meter():
    grade = _flat(10) + _climb(100) + _flat(10)
    hr    = constant_stream(160.0, len(grade))
    result = extract_climb_segments([], hr, grade)  # empty watts
    assert len(result) == 1
    assert result[0].avg_power_watts is None
    assert result[0].avg_hr_bpm      == pytest.approx(160.0)


def test_climbs_no_hr_monitor():
    grade = _flat(10) + _climb(100) + _flat(10)
    watts = constant_stream(250.0, len(grade))
    result = extract_climb_segments(watts, [], grade)  # empty heartrate
    assert len(result) == 1
    assert result[0].avg_hr_bpm      is None
    assert result[0].avg_power_watts == pytest.approx(250.0)


def test_climbs_custom_min_grade_and_duration():
    # 3% grade for 30 seconds: normally filtered out, but with min_grade=2 and
    # min_duration=20 it should be detected.
    grade = _flat(10) + constant_stream(3.0, 30) + _flat(10)
    watts = constant_stream(200.0, len(grade))
    result = extract_climb_segments(watts, [], grade, min_grade=2.0, min_duration=20)
    assert len(result) == 1
    assert result[0].duration_seconds == 30


# ---------------------------------------------------------------------------
# activity_metrics_from_dict
# ---------------------------------------------------------------------------

def test_from_dict_round_trip():
    original = ActivityMetrics(
        normalized_power=250.1,
        variability_index=1.05,
        time_in_zones={"Z1": 60, "Z2": 120, "Z3": 0, "Z4": 0, "Z5": 0, "Z6": 0},
        power_duration_curve={"5s": 400.0, "1m": 320.0, "5m": 280.0, "20m": 260.0, "60m": None},
        hr_decoupling_pct=3.2,
        climb_segments=[
            ClimbSegment(duration_seconds=90, avg_power_watts=270.0, avg_hr_bpm=155.0, avg_grade_pct=5.5)
        ],
    )
    as_dict = dataclasses.asdict(original)
    reconstructed = activity_metrics_from_dict(as_dict)
    assert reconstructed == original


def test_from_dict_nested_climb_segments_are_dataclasses():
    d = {
        "normalized_power": 200.0,
        "variability_index": 1.0,
        "time_in_zones": {"Z1": 10, "Z2": 0, "Z3": 0, "Z4": 0, "Z5": 0, "Z6": 0},
        "power_duration_curve": {},
        "hr_decoupling_pct": None,
        "climb_segments": [
            {"duration_seconds": 100, "avg_power_watts": 250.0, "avg_hr_bpm": 160.0, "avg_grade_pct": 5.0}
        ],
    }
    result = activity_metrics_from_dict(d)
    # Must be ClimbSegment instances, not raw dicts
    assert isinstance(result.climb_segments[0], ClimbSegment)


# ---------------------------------------------------------------------------
# compute_activity_metrics (integration / wiring smoke tests)
# ---------------------------------------------------------------------------

def test_activity_metrics_empty_streams():
    result = compute_activity_metrics({}, ftp=293.0)
    assert result.normalized_power  is None
    assert result.variability_index is None
    assert result.hr_decoupling_pct is None
    assert result.climb_segments    == []
    assert all(v == 0 for v in result.time_in_zones.values())
    assert all(v is None for v in result.power_duration_curve.values())


def test_activity_metrics_only_watts_stream():
    # With only watts, NP/VI/zones/PDC should be populated;
    # decoupling and climbs should be absent.
    streams = {"watts": constant_stream(200, 3600)}
    result = compute_activity_metrics(streams, ftp=200.0)
    assert result.normalized_power  is not None
    assert result.variability_index is not None
    assert result.hr_decoupling_pct is None
    assert result.climb_segments    == []
    assert sum(result.time_in_zones.values()) == 3600


def test_activity_metrics_all_streams_populated():
    n = 3600
    grade = constant_stream(1.0, n // 2) + constant_stream(5.0, n // 2)
    streams = {
        "watts":        constant_stream(200, n),
        "heartrate":    constant_stream(140, n),
        "grade_smooth": grade,
    }
    result = compute_activity_metrics(streams, ftp=200.0)
    assert result.normalized_power  is not None
    assert result.variability_index is not None
    assert result.hr_decoupling_pct is not None
    assert len(result.climb_segments) > 0


def test_activity_metrics_rounding():
    # NP → 1 decimal, VI → 3 decimals, PDC values → 1 decimal,
    # hr_decoupling_pct → 1 decimal. Use a stream where the expected values
    # are already whole numbers so rounding doesn't alter them.
    streams = {"watts": constant_stream(200, 3600)}
    result = compute_activity_metrics(streams, ftp=200.0)
    # NP for constant 200W = 200.0 — one decimal place is 200.0
    assert result.normalized_power == 200.0
    # VI = NP / avg = 200.0 / 200.0 = 1.0 — three decimal places is 1.0
    assert result.variability_index == 1.0
    # All PDC values should be 200.0 (one decimal)
    for label, val in result.power_duration_curve.items():
        if val is not None:
            assert val == 200.0, f"{label}: expected 200.0, got {val}"
