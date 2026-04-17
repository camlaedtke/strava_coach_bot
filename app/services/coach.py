"""
app/services/coach.py — Orchestration layer: Strava data + Claude coaching.

This is the "brain" of the bot. It sits between the Telegram router and the
individual service modules (strava.py, claude.py, metrics.py), wiring them
together into a single coaching reply:

  1. Fetch recent Strava activity summaries (last 10)
  2. For the 3 most recent cycling activities, load metrics from cache or fetch
     streams from Strava → compute → store (fetch-or-cache pattern)
  3. Build a rich system prompt: athlete profile + training context with
     NP, VI, zones, PDC, HR decoupling, and climb segments where available
  4. Call Claude with the prompt, history, and user's message

Design decisions:
  - ATHLETE_PROFILE lives here, not in claude.py. Coaching context (FTP, weight,
    training goals) belongs in the coaching layer, not the API plumbing layer.
  - fetch-or-cache: stream data is persisted to Supabase on first fetch so
    repeated messages don't burn Strava API rate limits.
  - asyncio.gather() fires all stream fetches for unseen activities concurrently
    instead of sequentially, reducing latency on cold-cache messages.
  - If Strava isn't connected or any step fails, we fall back to calling Claude
    without training context — the bot stays useful for general questions.
  - Activity formatting converts to imperial (miles, feet) since that's the
    athlete's native unit system.
"""

import asyncio
from datetime import datetime

import httpx

import app.services.metrics as metrics_module
from app.models.schemas import ConversationMessage, StravaActivitySummary
from app.services.claude import get_claude_reply
from app.services.metrics import ActivityMetrics
from app.services.strava import get_activity_streams, get_recent_activities, get_valid_token
from app.services import supabase as supabase_service

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Athlete's FTP — used for zone calculations in metrics.py.
# Stored here (not in metrics.py) because it's coaching context, not math.
# Update this as FTP improves.
FTP = 290  # watts

# Number of recent activities to fetch full stream data for.
# Each unseen activity costs 1 Strava API call. Cache hits are free.
STREAM_ACTIVITY_COUNT = 3

# Cycling activity types in Strava's taxonomy.
_CYCLING_TYPES = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide"}


# ---------------------------------------------------------------------------
# Athlete profile (static system prompt base)
# ---------------------------------------------------------------------------

# This is the coaching persona Claude adopts for every reply.
#
# PROMPT CACHING: This constant is the static part of the system prompt.
# The dynamic part (recent training data) is appended per-request. Together
# they need to reach ~1024 tokens for Anthropic's cache_control to activate.
# As this profile grows (coaching philosophy, injury notes, target events),
# it will cross that threshold and cache hits will reduce cost by ~90%.
ATHLETE_PROFILE = """You are an expert cycling coach and training advisor. \
Your athlete is a competitive road and gravel cyclist with the following profile:

- FTP: ~290 watts (constantly improving — treat this as approximate)
- Body weight: ~164 lbs (74 kg) → ~3.92 W/kg at FTP
- Weekly training: 7–15 hours depending on the block
- Training style: coach-directed with structured threshold and VO2max blocks
- Goals: performance in road and gravel events

When answering questions:
- Be specific and data-driven. Reference watts, W/kg, TSS, duration, and \
elevation where relevant.
- Keep replies concise but complete — this is a Telegram chat, not a report.
- If the athlete asks about a recent ride or training week, use the activity \
data provided in this prompt and give actionable feedback.
- Use plain language. Avoid jargon unless the athlete uses it first.
- If you don't have enough information to give a confident answer, say so and \
ask a clarifying question.
- Zone reference (Coggan 6-zone, based on ~290W FTP):
    Z1 (Recovery):   < 160W  (< 55% FTP)
    Z2 (Endurance):  160–218W  (55–75%)
    Z3 (Tempo):      218–261W  (75–90%)
    Z4 (Threshold):  261–305W  (90–105%)
    Z5 (VO2max):     305–348W  (105–120%)
    Z6 (Anaerobic):  > 348W   (> 120%)
"""


# ---------------------------------------------------------------------------
# Unit conversion helpers
# ---------------------------------------------------------------------------

def _seconds_to_hhmm(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def _meters_to_miles(meters: float) -> float:
    return meters / 1609.344


def _meters_to_feet(meters: float) -> float:
    return meters * 3.28084


def _format_date(iso_str: str) -> str:
    """Parse a Strava ISO 8601 UTC timestamp and return a short date string."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%a %b %-d")  # e.g. "Tue Apr 15"
    except ValueError:
        return iso_str


# ---------------------------------------------------------------------------
# Activity formatting
# ---------------------------------------------------------------------------

def _format_activity(activity: StravaActivitySummary) -> str:
    """
    Format an activity with aggregate fields only (no stream data).

    Used for the 7 older activities that don't get stream fetches, or as a
    fallback when stream fetching fails for a recent activity.
    """
    lines = [
        f"**{activity.name}** ({activity.type}) — {_format_date(activity.start_date)}",
        (
            f"Duration: {_seconds_to_hhmm(activity.moving_time)}  |  "
            f"Distance: {_meters_to_miles(activity.distance):.1f} mi  |  "
            f"Elevation: {_meters_to_feet(activity.total_elevation_gain):,.0f} ft"
        ),
    ]

    power_parts = []
    if activity.average_watts is not None:
        w_per_kg = activity.average_watts / 74.0
        power_parts.append(f"Avg Power: {activity.average_watts:.0f}W ({w_per_kg:.2f} W/kg)")
    if activity.weighted_average_watts is not None:
        power_parts.append(f"NP (Strava est.): {activity.weighted_average_watts}W")
    if power_parts:
        lines.append("  |  ".join(power_parts))

    hr_parts = []
    if activity.average_heartrate is not None:
        hr_parts.append(f"Avg HR: {activity.average_heartrate:.0f} bpm")
    if activity.max_heartrate is not None:
        hr_parts.append(f"Max HR: {activity.max_heartrate:.0f} bpm")
    if hr_parts:
        lines.append("  |  ".join(hr_parts))

    return "\n".join(lines)


def _format_rich_activity(
    activity: StravaActivitySummary,
    metrics: ActivityMetrics,
) -> str:
    """
    Format an activity with full computed metrics from stream data.

    Sections where all values are None are omitted so the output is clean for
    activities without power meters or HR monitors.
    """
    lines = [
        f"**{activity.name}** ({activity.type}) — {_format_date(activity.start_date)}",
        (
            f"Duration: {_seconds_to_hhmm(activity.moving_time)}  |  "
            f"Distance: {_meters_to_miles(activity.distance):.1f} mi  |  "
            f"Elevation: {_meters_to_feet(activity.total_elevation_gain):,.0f} ft"
        ),
    ]

    # Power line: avg + NP + VI
    power_parts = []
    if activity.average_watts is not None:
        w_per_kg = activity.average_watts / 74.0
        power_parts.append(f"Avg Power: {activity.average_watts:.0f}W ({w_per_kg:.2f} W/kg)")
    if metrics.normalized_power is not None:
        power_parts.append(f"NP: {metrics.normalized_power:.0f}W")
    if metrics.variability_index is not None:
        power_parts.append(f"VI: {metrics.variability_index:.2f}")
    if power_parts:
        lines.append("  |  ".join(power_parts))

    # HR line: avg + max + decoupling
    hr_parts = []
    if activity.average_heartrate is not None:
        hr_parts.append(f"Avg HR: {activity.average_heartrate:.0f} bpm")
    if activity.max_heartrate is not None:
        hr_parts.append(f"Max HR: {activity.max_heartrate:.0f} bpm")
    if metrics.hr_decoupling_pct is not None:
        sign = "+" if metrics.hr_decoupling_pct > 0 else ""
        hr_parts.append(f"HR Decoupling: {sign}{metrics.hr_decoupling_pct:.1f}%")
    if hr_parts:
        lines.append("  |  ".join(hr_parts))

    # Time in zones — show all 6 zones, skip if all zeros (no power data)
    zones = metrics.time_in_zones
    if zones and any(v > 0 for v in zones.values()):
        zone_parts = [
            f"{z}: {zones.get(z, 0) // 60}m"
            for z in ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6"]
        ]
        lines.append("Time in Zones: " + " | ".join(zone_parts))

    # Power duration curve
    pdc = metrics.power_duration_curve
    if pdc and any(v is not None for v in pdc.values()):
        pdc_parts = [
            f"{label}: {val:.0f}W"
            for label in ["5s", "1m", "5m", "20m", "60m"]
            if (val := pdc.get(label)) is not None
        ]
        if pdc_parts:
            lines.append("Power Curve: " + " | ".join(pdc_parts))

    # Climb segments (cap at 3 to avoid prompt bloat)
    if metrics.climb_segments:
        climb_strs = []
        for cs in metrics.climb_segments[:3]:
            dur_min = cs.duration_seconds // 60
            seg_parts = [f"{dur_min}min @ {cs.avg_grade_pct:.1f}%"]
            if cs.avg_power_watts is not None:
                seg_parts.append(f"{cs.avg_power_watts:.0f}W")
            if cs.avg_hr_bpm is not None:
                seg_parts.append(f"{cs.avg_hr_bpm:.0f}bpm")
            climb_strs.append(" — ".join(seg_parts))
        n = len(metrics.climb_segments)
        lines.append(f"Climbs ({n}): " + "; ".join(climb_strs))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Training context builder
# ---------------------------------------------------------------------------

def _build_training_context(
    activities: list[StravaActivitySummary],
    metrics_by_id: dict[int, ActivityMetrics],
) -> str:
    """
    Format all recent cycling activities into a training context block.

    Activities in metrics_by_id get the rich format (NP, zones, PDC, etc.).
    The remaining activities get the aggregate-only format as a lightweight
    summary.
    """
    rides = [a for a in activities if a.type in _CYCLING_TYPES]

    if not rides:
        return "No recent cycling activities found."

    formatted = []
    for ride in rides:
        metrics = metrics_by_id.get(ride.id)
        if metrics is not None:
            formatted.append(_format_rich_activity(ride, metrics))
        else:
            formatted.append(_format_activity(ride))

    return "\n\n".join(formatted)


def _build_system_prompt(training_context: str | None) -> str:
    """
    Assemble the full system prompt: static athlete profile + dynamic training data.
    """
    if training_context is not None:
        return (
            ATHLETE_PROFILE
            + "\n\n## Recent Training (last 10 cycling activities)\n\n"
            + training_context
        )
    return (
        ATHLETE_PROFILE
        + "\n\n(Strava is not connected or data is temporarily unavailable. "
        "Answer general coaching questions as best you can without specific "
        "activity data.)"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def get_coaching_reply(
    telegram_user_id: int,
    user_message: str,
    history: list[ConversationMessage] | None = None,
) -> str:
    """
    Fetch Strava data, build a grounded system prompt, and return Claude's reply.

    This is the single function the Telegram router calls. It orchestrates:
      1. Fetch recent activity summaries (last 10)
      2. For the 3 most recent cycling activities: load metrics from cache or
         fetch streams → compute → store (fetch-or-cache with asyncio.gather)
      3. Build system prompt = athlete profile + training context
      4. Call Claude with prompt, history, and user message

    Gracefully handles Strava not being connected or API failures — Claude
    still replies without training context rather than crashing.

    Args:
        telegram_user_id: Identifies the user in strava_tokens and activity_metrics.
        user_message: Raw text from Telegram.
        history: Prior conversation turns from Supabase (oldest-first).
    """
    training_context: str | None = None

    try:
        raw_activities = await get_recent_activities(
            telegram_user_id=telegram_user_id,
            per_page=10,
        )

        # Parse raw dicts into typed models; skip malformed rows
        activities: list[StravaActivitySummary] = []
        for raw in raw_activities:
            try:
                activities.append(StravaActivitySummary(**raw))
            except Exception as e:
                print(f"coach: skipping malformed activity (id={raw.get('id')}): {e}")

        # The STREAM_ACTIVITY_COUNT most recent cycling activities get stream analysis
        rides = [a for a in activities if a.type in _CYCLING_TYPES]
        rides_for_streams = rides[:STREAM_ACTIVITY_COUNT]

        # --- Fetch-or-cache ---
        # Check the DB for each activity. Collect cache misses for batch fetching.
        metrics_by_id: dict[int, ActivityMetrics] = {}
        cache_misses: list[StravaActivitySummary] = []

        for ride in rides_for_streams:
            cached = await supabase_service.get_cached_metrics(ride.id)
            if cached is not None:
                metrics_by_id[ride.id] = cached
            else:
                cache_misses.append(ride)

        if cache_misses:
            # Get a valid token once and reuse it for all stream fetches.
            # This avoids N separate DB reads for the same token.
            access_token = await get_valid_token(telegram_user_id)

            # Fire all stream fetches concurrently.
            #
            # asyncio.gather() takes a list of coroutines and runs them at the
            # same time within the event loop — not in parallel threads, but
            # interleaved: while one awaits a network response, another can run.
            # For N=3 ~100ms Strava calls, this saves ~200ms vs sequential.
            #
            # return_exceptions=True prevents one failed fetch from cancelling
            # the others. Instead, failed calls return the Exception object as
            # their result, which we check for below.
            stream_results = await asyncio.gather(
                *[
                    get_activity_streams(ride.id, access_token)
                    for ride in cache_misses
                ],
                return_exceptions=True,
            )

            for ride, result in zip(cache_misses, stream_results):
                if isinstance(result, Exception):
                    print(f"coach: stream fetch failed for activity {ride.id}: {result}")
                    continue

                # Compute metrics and persist both streams and metrics
                computed = metrics_module.compute_activity_metrics(result, ftp=FTP)
                metrics_by_id[ride.id] = computed
                await supabase_service.save_activity_metrics(
                    activity_id=ride.id,
                    telegram_user_id=telegram_user_id,
                    streams=result,
                    metrics=computed,
                )

        training_context = _build_training_context(activities, metrics_by_id)

    except ValueError:
        # User hasn't completed the Strava OAuth flow yet.
        print(f"coach: no Strava tokens for user {telegram_user_id}, proceeding without data")

    except httpx.HTTPStatusError as e:
        print(f"coach: Strava API error for user {telegram_user_id}: {e.response.status_code} {e}")

    except Exception as e:
        print(f"coach: unexpected error for user {telegram_user_id}: {e}")

    system_prompt = _build_system_prompt(training_context)

    return await get_claude_reply(
        user_message=user_message,
        history=history,
        system_prompt=system_prompt,
    )
