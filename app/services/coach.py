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
from app.models.schemas import AthleteProfile, ConversationMessage, StravaActivitySummary
from app.services.claude import get_claude_reply
from app.services.metrics import ActivityMetrics
from app.services.strava import get_activity_streams, get_recent_activities, get_valid_token
from app.services import supabase as supabase_service

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of recent activities to fetch full stream data for.
# Each unseen activity costs 1 Strava API call. Cache hits are free.
STREAM_ACTIVITY_COUNT = 5

# Cycling activity types in Strava's taxonomy.
_CYCLING_TYPES = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide"}


# ---------------------------------------------------------------------------
# Athlete profile system prompt builder
# ---------------------------------------------------------------------------

def _build_athlete_profile_text(ftp: int, weight_kg: float) -> str:
    """
    Build the coaching persona section of the system prompt from live profile values.

    Called once per request using values fetched from the athlete_profile table
    (falling back to AthleteProfile defaults when no row exists). Generating this
    per-request rather than as a module constant means FTP and weight changes
    take effect immediately without redeploying.

    PROMPT CACHING: This text forms the static-ish part of the system prompt.
    The dynamic part (recent training data) is appended per-request. Together
    they need ~1024 tokens for Anthropic's cache_control to activate.
    """
    lbs = round(weight_kg * 2.20462)
    return f"""You are an expert cycling coach and training advisor. \
Your athlete is a competitive road and gravel cyclist with the following profile:

- FTP: ~{ftp} watts (constantly improving — treat this as approximate)
- Body weight: ~{lbs} lbs ({weight_kg} kg) → ~{ftp / weight_kg:.2f} W/kg at FTP
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
- Zone reference (Coggan 6-zone, based on {ftp}W FTP):
    Z1 (Recovery):   < {round(ftp * 0.55)}W  (< 55% FTP)
    Z2 (Endurance):  {round(ftp * 0.55)}–{round(ftp * 0.75)}W  (55–75%)
    Z3 (Tempo):      {round(ftp * 0.75)}–{round(ftp * 0.90)}W  (75–90%)
    Z4 (Threshold):  {round(ftp * 0.90)}–{round(ftp * 1.05)}W  (90–105%)
    Z5 (VO2max):     {round(ftp * 1.05)}–{round(ftp * 1.20)}W  (105–120%)
    Z6 (Anaerobic):  > {round(ftp * 1.20)}W   (> 120%)
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

def _format_activity(activity: StravaActivitySummary, weight_kg: float) -> str:
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
        w_per_kg = activity.average_watts / weight_kg
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
    weight_kg: float,
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
        w_per_kg = activity.average_watts / weight_kg
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
# Power PR formatter
# ---------------------------------------------------------------------------

# 5s is intentionally omitted — sprint peaks vary wildly by ride type and are
# less meaningful as an all-time record compared to sustained-effort durations.
_PR_LABELS = [
    "15s", "30s", "1m", "2m", "3m", "5m", "10m", "15m", "20m", "30m", "45m", "60m"
]


def _format_power_prs(prs: dict) -> str:
    """Format all-time power PRs as a compact line for the system prompt."""
    parts = [
        f"{label}: {prs[label]:.0f}W"
        for label in _PR_LABELS
        if label in prs and prs[label] is not None
    ]
    return " | ".join(parts) if parts else "(no power data yet)"


# ---------------------------------------------------------------------------
# Training context builder
# ---------------------------------------------------------------------------

def _build_training_context(
    activities: list[StravaActivitySummary],
    metrics_by_id: dict[int, ActivityMetrics],
    weight_kg: float,
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
            formatted.append(_format_rich_activity(ride, metrics, weight_kg))
        else:
            formatted.append(_format_activity(ride, weight_kg))

    return "\n\n".join(formatted)


def _build_system_prompt(
    training_context: str | None,
    power_prs: dict | None,
    profile: AthleteProfile,
) -> str:
    """
    Assemble the full system prompt: athlete profile text + PRs + dynamic training data.
    """
    prompt = _build_athlete_profile_text(profile.ftp_watts, profile.weight_kg)
    if power_prs:
        prompt += "\n\n## All-Time Power Records\n" + _format_power_prs(power_prs)
    if training_context is not None:
        return (
            prompt
            + "\n\n## Recent Training (last 10 cycling activities)\n\n"
            + training_context
        )
    return (
        prompt
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
    # Fetch the athlete profile early so FTP/weight are available for both
    # metric computation and system prompt generation. Fall back to defaults
    # on any DB error so the bot stays functional.
    profile = AthleteProfile()
    try:
        profile = await supabase_service.get_athlete_profile(telegram_user_id)
    except Exception as e:
        print(f"coach: failed to fetch athlete profile for {telegram_user_id}: {e}")

    training_context: str | None = None
    power_prs: dict | None = None

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
                computed = metrics_module.compute_activity_metrics(result, ftp=profile.ftp_watts)
                metrics_by_id[ride.id] = computed
                await supabase_service.save_activity_metrics(
                    activity_id=ride.id,
                    telegram_user_id=telegram_user_id,
                    streams=result,
                    metrics=computed,
                )
                await supabase_service.upsert_power_prs(
                    telegram_user_id=telegram_user_id,
                    new_prs=computed.power_duration_curve,
                )

        training_context = _build_training_context(activities, metrics_by_id, profile.weight_kg)
        power_prs = await supabase_service.get_power_prs(telegram_user_id)

    except ValueError:
        # User hasn't completed the Strava OAuth flow yet.
        print(f"coach: no Strava tokens for user {telegram_user_id}, proceeding without data")

    except httpx.HTTPStatusError as e:
        print(f"coach: Strava API error for user {telegram_user_id}: {e.response.status_code} {e}")

    except Exception as e:
        print(f"coach: unexpected error for user {telegram_user_id}: {e}")

    system_prompt = _build_system_prompt(training_context, power_prs, profile)

    return await get_claude_reply(
        user_message=user_message,
        history=history,
        system_prompt=system_prompt,
    )
