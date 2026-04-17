"""
app/services/metrics.py — Pure cycling metric computation from stream data.

All functions here are stateless and synchronous: they take plain Python lists
(from Strava's per-second streams) and return floats, dicts, or dataclasses.
No HTTP, no database, no async. This makes them easy to test in isolation and
keeps computation separate from I/O.

Called by coach.py after stream data is fetched (or loaded from cache).

All computations assume ~1Hz stream data (one sample per second), which is
the default for Garmin/Wahoo devices in "every second" recording mode. Devices
using "smart recording" (variable intervals) will produce slightly inaccurate
results for time-based windows (NP, PDC) but the difference is small in practice.

Metric reference:
  NP/VI: Coggan & Allen, "Training and Racing with a Power Meter"
  Zone model: Coggan 6-zone model based on % of FTP
  HR decoupling: also known as Aerobic Decoupling (Aerobic Efficiency)
"""

import dataclasses
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ClimbSegment:
    """
    A sustained climb extracted from the grade_smooth stream.

    'Sustained' means >= min_grade % for >= min_duration seconds (both
    configurable in extract_climb_segments). Short punchy efforts and false
    flats are filtered out.
    """
    duration_seconds: int
    avg_power_watts: float | None    # None if no power meter on this ride
    avg_hr_bpm: float | None         # None if no HR monitor
    avg_grade_pct: float


@dataclass
class ActivityMetrics:
    """
    All computed metrics for a single ride.

    Fields are None when the required stream was unavailable (no power meter,
    no HR monitor, too short for the computation). Callers should always check
    for None before displaying or analyzing these values.

    This dataclass is designed for JSON round-trips: `dataclasses.asdict()`
    serializes it for Supabase storage, and `activity_metrics_from_dict()`
    reconstructs it on read (handling the nested ClimbSegment objects).
    """
    normalized_power: float | None
    variability_index: float | None
    time_in_zones: dict[str, int]                    # seconds per zone: Z1–Z6
    power_duration_curve: dict[str, float | None]    # best avg watts: 5s/1m/5m/20m/60m
    hr_decoupling_pct: float | None
    climb_segments: list[ClimbSegment]


def activity_metrics_from_dict(d: dict) -> ActivityMetrics:
    """
    Reconstruct an ActivityMetrics instance from a plain dict.

    Used when loading from Supabase JSONB storage. dataclasses.asdict()
    flattens nested dataclasses to dicts, so we need to manually reconstruct
    the nested ClimbSegment objects.
    """
    climb_segments = [ClimbSegment(**cs) for cs in d.get("climb_segments", [])]
    return ActivityMetrics(
        normalized_power=d.get("normalized_power"),
        variability_index=d.get("variability_index"),
        time_in_zones=d.get("time_in_zones", {}),
        power_duration_curve=d.get("power_duration_curve", {}),
        hr_decoupling_pct=d.get("hr_decoupling_pct"),
        climb_segments=climb_segments,
    )


# ---------------------------------------------------------------------------
# Zone model
# ---------------------------------------------------------------------------

# Coggan 6-zone model: boundaries as fractions of FTP.
# Each tuple is (zone_name, lower_bound_fraction, upper_bound_fraction).
# The lower bound is inclusive, upper bound is exclusive.
_ZONE_BOUNDARIES = [
    ("Z1", 0.0,  0.55),   # Active Recovery
    ("Z2", 0.55, 0.75),   # Endurance
    ("Z3", 0.75, 0.90),   # Tempo
    ("Z4", 0.90, 1.05),   # Lactate Threshold
    ("Z5", 1.05, 1.20),   # VO2max
    ("Z6", 1.20, float("inf")),  # Anaerobic / Neuromuscular
]


# ---------------------------------------------------------------------------
# Individual metric functions
# ---------------------------------------------------------------------------

def compute_normalized_power(watts: list[float]) -> float | None:
    """
    Compute Normalized Power (NP) from a per-second watts stream.

    Algorithm (Coggan):
      1. Compute a 30-second rolling average of the raw watts.
      2. Raise each rolling average to the 4th power.
      3. Take the mean of those 4th powers.
      4. Take the 4th root of that mean.

    The 4th power transform penalizes highly variable efforts more than steady
    ones — this reflects the non-linear physiological cost of intensity spikes.
    A flat 200W ride has lower NP than a ride averaging 200W with big surges.

    Implementation note: we use a sliding window sum (O(n)) rather than
    recomputing the sum for each window (O(n * window_size)). For a 2-hour
    ride at 1Hz (7200 samples), the naive approach would do 7200 * 30 = 216k
    additions; the sliding approach does 7200.

    Returns None if there are fewer than 30 data points.
    """
    n = len(watts)
    if n < 30:
        return None

    window_size = 30
    window_sum = sum(watts[:window_size])
    rolling_avgs = [window_sum / window_size]

    for i in range(window_size, n):
        window_sum += watts[i] - watts[i - window_size]
        rolling_avgs.append(window_sum / window_size)

    mean_fourth = sum(w ** 4 for w in rolling_avgs) / len(rolling_avgs)
    return mean_fourth ** 0.25


def compute_variability_index(np_watts: float, avg_watts: float) -> float | None:
    """
    Variability Index (VI) = NP / average_power.

    Measures how "steady" the effort was:
      - VI = 1.00: perfectly steady (rare, typically only on an indoor trainer)
      - VI = 1.05: very steady (typical for a good flat TT)
      - VI > 1.10: variable (typical for a hilly ride or crit)
      - VI > 1.15 on a flat ride: suggests poor pacing or surging

    Returns None if avg_watts is zero (e.g., entire ride at rest).
    """
    if avg_watts == 0:
        return None
    return np_watts / avg_watts


def compute_time_in_zones(watts: list[float], ftp: float) -> dict[str, int]:
    """
    Compute time (in seconds) spent in each of the 6 Coggan power zones.

    Each sample in the watts stream is assumed to be 1 second, so the count
    per zone equals the seconds per zone. Zero-power samples (coasting, stops)
    fall in Z1, which is correct — they represent full recovery and are part
    of the aerobic cost picture.

    Args:
        watts: Per-second watts values.
        ftp: Athlete's Functional Threshold Power in watts.

    Returns:
        Dict mapping zone name to seconds: {"Z1": 300, "Z2": 1800, ...}
    """
    counts: dict[str, int] = {zone: 0 for zone, _, _ in _ZONE_BOUNDARIES}
    for w in watts:
        frac = w / ftp if ftp > 0 else 0.0
        for zone, lo, hi in _ZONE_BOUNDARIES:
            if lo <= frac < hi:
                counts[zone] += 1
                break
    return counts


def _best_average_power(watts: list[float], duration: int) -> float | None:
    """
    Find the best (highest) average power over any window of `duration` seconds.

    Uses a sliding window sum for O(n) performance. Returns None if the ride
    is shorter than the requested duration.
    """
    n = len(watts)
    if n < duration:
        return None

    window_sum = sum(watts[:duration])
    best_sum = window_sum
    for i in range(duration, n):
        window_sum += watts[i] - watts[i - duration]
        if window_sum > best_sum:
            best_sum = window_sum

    return best_sum / duration


def compute_power_duration_curve(watts: list[float]) -> dict[str, float | None]:
    """
    Compute the best average power for key durations: 5s, 1m, 5m, 20m, 60m.

    These five points sketch the athlete's power profile:
      - 5s:  neuromuscular / sprint peak
      - 1m:  anaerobic capacity
      - 5m:  VO2max (MAP proxy)
      - 20m: lactate threshold proxy (often used to estimate FTP as 95% × 20m)
      - 60m: true sustained threshold

    Returns None for each duration where the ride is shorter than the window.
    """
    durations = [
        ("5s",  5),
        ("1m",  60),
        ("5m",  300),
        ("20m", 1200),
        ("60m", 3600),
    ]
    return {label: _best_average_power(watts, secs) for label, secs in durations}


def compute_hr_decoupling(
    watts: list[float],
    heartrate: list[float],
) -> float | None:
    """
    Compute HR decoupling (also called Aerobic Decoupling or Aerobic Efficiency).

    Splits the ride in half and compares the power:HR efficiency ratio of the
    first half to the second half:

        decoupling = (EF_first - EF_second) / EF_first × 100

    Where EF (Efficiency Factor) = avg_power / avg_HR for that half.

    Interpretation:
      - Positive value: HR rose relative to power in the second half → aerobic
        drift → the athlete was working harder physiologically than the watts
        suggest. > 5% is a common threshold for "significant" decoupling.
      - Negative value: HR actually fell relative to power → uncommon, sometimes
        seen with cardiac drift or caffeine effects.
      - Near 0%: well-paced aerobic effort with stable HR.

    Zero-power and zero-HR samples are excluded (coasting, HR dropouts).
    Returns None if either half has fewer than 10 valid paired samples.
    """
    n = min(len(watts), len(heartrate))
    if n < 60:
        return None

    mid = n // 2

    def efficiency_factor(w_slice: list[float], hr_slice: list[float]) -> float | None:
        pairs = [
            (w, h) for w, h in zip(w_slice, hr_slice)
            if w > 0 and h > 0
        ]
        if len(pairs) < 10:
            return None
        avg_w = sum(p[0] for p in pairs) / len(pairs)
        avg_h = sum(p[1] for p in pairs) / len(pairs)
        return avg_w / avg_h if avg_h > 0 else None

    ef_first  = efficiency_factor(watts[:mid],  heartrate[:mid])
    ef_second = efficiency_factor(watts[mid:n], heartrate[mid:n])

    if ef_first is None or ef_second is None or ef_first == 0:
        return None

    return (ef_first - ef_second) / ef_first * 100


def extract_climb_segments(
    watts: list[float],
    heartrate: list[float],
    grade_smooth: list[float],
    min_grade: float = 4.0,
    min_duration: int = 60,
) -> list[ClimbSegment]:
    """
    Find sustained climbs in the grade_smooth stream and summarize each one.

    A "climb" is defined as a contiguous run where grade_smooth >= min_grade %
    for at least min_duration consecutive seconds. Short kicks and false flats
    are filtered out.

    Args:
        watts: Per-second power values (may be empty if no power meter).
        heartrate: Per-second HR values (may be empty if no HR monitor).
        grade_smooth: Per-second smoothed grade % from Strava.
        min_grade: Minimum grade percentage to qualify as a climb (default 4%).
        min_duration: Minimum sustained duration in seconds (default 60s = 1 min).

    Returns:
        List of ClimbSegment objects, in order of appearance.
    """
    if not grade_smooth:
        return []

    def make_segment(start: int, end: int) -> ClimbSegment | None:
        seg_len = end - start
        if seg_len < min_duration:
            return None

        w_seg = watts[start:end]     if watts      else []
        hr_seg = heartrate[start:end] if heartrate  else []
        g_seg = grade_smooth[start:end]

        avg_w    = sum(w_seg)  / len(w_seg)  if w_seg  else None
        avg_hr   = sum(hr_seg) / len(hr_seg) if hr_seg else None
        avg_grade = sum(g_seg) / len(g_seg)

        return ClimbSegment(
            duration_seconds=seg_len,
            avg_power_watts=round(avg_w, 1)    if avg_w    is not None else None,
            avg_hr_bpm=round(avg_hr, 1)        if avg_hr   is not None else None,
            avg_grade_pct=round(avg_grade, 1),
        )

    segments: list[ClimbSegment] = []
    in_climb = False
    climb_start = 0

    for i, grade in enumerate(grade_smooth):
        if grade >= min_grade and not in_climb:
            in_climb = True
            climb_start = i
        elif grade < min_grade and in_climb:
            in_climb = False
            seg = make_segment(climb_start, i)
            if seg is not None:
                segments.append(seg)

    # Handle the case where the ride ends while still on a climb
    if in_climb:
        seg = make_segment(climb_start, len(grade_smooth))
        if seg is not None:
            segments.append(seg)

    return segments


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def compute_activity_metrics(streams: dict[str, list], ftp: float) -> ActivityMetrics:
    """
    Compute all metrics from a streams dict and return an ActivityMetrics object.

    This is the single function coach.py calls after fetching stream data.
    It extracts the relevant arrays, handles missing streams gracefully (not
    all devices have power meters or HR monitors), and delegates to the
    individual compute_* functions.

    Args:
        streams: Dict from get_activity_streams(), keyed by stream type.
                 Expected keys: "watts", "heartrate", "grade_smooth".
                 Missing keys produce None metrics for computations that need them.
        ftp: Athlete's FTP in watts, used for zone boundary calculations.

    Returns:
        ActivityMetrics with all computed values (None where stream was absent).
    """
    # Extract and normalize streams to float lists
    # Strava returns int or float depending on the stream type; standardize to float
    watts       = [float(w) for w in streams.get("watts", [])]
    heartrate   = [float(h) for h in streams.get("heartrate", [])]
    grade_smooth = [float(g) for g in streams.get("grade_smooth", [])]

    # NP and VI
    np_watts = compute_normalized_power(watts) if watts else None
    avg_watts = sum(watts) / len(watts) if watts else 0.0
    vi = compute_variability_index(np_watts, avg_watts) if np_watts is not None else None

    # Time in zones (all zeros if no power data)
    zones = (
        compute_time_in_zones(watts, ftp)
        if watts
        else {zone: 0 for zone, _, _ in _ZONE_BOUNDARIES}
    )

    # Power duration curve (all None if no power data)
    pdc = (
        compute_power_duration_curve(watts)
        if watts
        else {"5s": None, "1m": None, "5m": None, "20m": None, "60m": None}
    )

    # HR decoupling (requires both power and HR)
    decoupling = (
        compute_hr_decoupling(watts, heartrate)
        if watts and heartrate
        else None
    )

    # Climb segments (requires grade_smooth; power/HR are optional per-segment)
    climbs = (
        extract_climb_segments(watts, heartrate, grade_smooth)
        if grade_smooth
        else []
    )

    return ActivityMetrics(
        normalized_power=round(np_watts, 1) if np_watts is not None else None,
        variability_index=round(vi, 3)      if vi is not None       else None,
        time_in_zones=zones,
        power_duration_curve={
            k: round(v, 1) if v is not None else None
            for k, v in pdc.items()
        },
        hr_decoupling_pct=round(decoupling, 1) if decoupling is not None else None,
        climb_segments=climbs,
    )
