"""
scripts/backfill_activities.py — One-time backfill of Strava activity streams and metrics.

Fetches up to ACTIVITIES_TO_FETCH recent Strava activities across multiple pages,
computes metrics for any not already cached in `activity_metrics`, and saves both raw
streams and computed metrics to Supabase.

Usage (from project root, with venv active and .env present):
    python scripts/backfill_activities.py

Rate limit note:
    Strava allows 100 requests/15 min and 1000/day.
    700 stream fetches on a cold start takes ~105 minutes to complete because the script
    pauses automatically when it gets a 429 and retries after the window resets.
    Re-runs are fast — cached activities are skipped entirely.
"""

import asyncio
import sys
import time
from pathlib import Path

import httpx

# Make sure the project root is on sys.path so `app.*` imports resolve when running
# this script directly. Without this, Python can't find the `app` package unless
# you add the project root to PYTHONPATH manually.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# app.config must be imported before any app.services imports so that pydantic-settings
# loads the .env file before the service modules read environment variables.
import app.config  # noqa: F401
from app.models.schemas import StravaActivitySummary
from app.services import strava as strava_service
from app.services import supabase as supabase_service
import app.services.metrics as metrics_module

# ---------------------------------------------------------------------------
# Constants — keep FTP in sync with coach.py
# ---------------------------------------------------------------------------

CYCLING_TYPES = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide"}
FTP = 290               # watts
ACTIVITIES_TO_FETCH = 1000
PAGE_SIZE = 200         # Strava's maximum per_page value
CONCURRENCY = 5         # simultaneous stream fetches — low enough to avoid 429 bursts
RETRY_WAIT_SECONDS = 905  # how long to wait on a 429 (just over Strava's 15-min window)


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------

async def _fetch_all_activity_summaries(telegram_user_id: int) -> list[dict]:
    """
    Fetch up to ACTIVITIES_TO_FETCH activity summaries using paginated API calls.

    Strava's /athlete/activities endpoint returns at most 200 rows per request.
    For 700 activities we need up to 4 pages (200 + 200 + 200 + 100).

    We stop early if Strava returns an empty page, which means we've reached the
    beginning of the athlete's activity history.

    Args:
        telegram_user_id: Used by get_valid_token() to retrieve credentials.

    Returns:
        A flat list of raw activity dicts (up to ACTIVITIES_TO_FETCH of them).
    """
    # get_valid_token() handles expiry and refresh transparently.
    access_token = await strava_service.get_valid_token(telegram_user_id)
    client = strava_service._get_http_client()

    all_activities: list[dict] = []
    page = 1

    while len(all_activities) < ACTIVITIES_TO_FETCH:
        remaining = ACTIVITIES_TO_FETCH - len(all_activities)
        per_page = min(PAGE_SIZE, remaining)

        print(f"  Fetching page {page} ({per_page} activities)...")
        # 30s timeout — Strava can be slow returning 200-row pages.
        # httpx's default is 5s, which is too tight for large payloads.
        response = await client.get(
            f"{strava_service.STRAVA_API_BASE}/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": per_page, "page": page},
            timeout=30.0,
        )
        response.raise_for_status()
        page_data = response.json()

        if not page_data:
            # Empty page = no more activities in Strava's history
            break

        all_activities.extend(page_data)
        page += 1

    return all_activities


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_telegram_user_id() -> int:
    """
    Look up the Telegram user ID from the strava_tokens table.

    This is a personal single-user bot, so we just take the first (only) row.
    """
    client = await supabase_service._get_client()
    result = (
        await client.table("strava_tokens")
        .select("telegram_user_id")
        .limit(1)
        .execute()
    )
    if not result.data:
        raise RuntimeError(
            "No rows found in strava_tokens. "
            "Complete the Strava OAuth flow via the bot first (/strava)."
        )
    return result.data[0]["telegram_user_id"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    # Step 1: Find the user
    telegram_user_id = await _get_telegram_user_id()
    print(f"Using telegram_user_id={telegram_user_id}\n")

    # Step 2: Fetch activity summaries (paginated)
    print(f"Fetching up to {ACTIVITIES_TO_FETCH} activities from Strava...")
    raw_activities = await _fetch_all_activity_summaries(telegram_user_id)

    # Parse into typed models, skip any malformed rows
    activities: list[StravaActivitySummary] = []
    for raw in raw_activities:
        try:
            activities.append(StravaActivitySummary(**raw))
        except Exception as e:
            print(f"  [skip] malformed activity id={raw.get('id')}: {e}")

    rides = [a for a in activities if a.type in CYCLING_TYPES]
    print(f"\nFound {len(rides)} cycling activities out of {len(activities)} total.\n")

    # Step 3: Identify cache misses — check DB for each activity
    print("Checking cache...")
    cache_misses: list[StravaActivitySummary] = []
    for ride in rides:
        cached = await supabase_service.get_cached_metrics(ride.id)
        if cached is not None:
            print(f"  [cached]     {ride.name}")
        else:
            cache_misses.append(ride)

    if not cache_misses:
        print("\nAll activities already cached. Nothing to do.")
        return

    print(f"\n{len(cache_misses)} cache miss(es) — fetching streams...\n")

    # Step 4: Fetch streams and save metrics, with bounded concurrency.
    #
    # We get a valid token once and pass it to all stream fetches.
    # This avoids re-reading the DB for every request.
    #
    # On 429 (rate limited): the fetch_and_save coroutine waits RETRY_WAIT_SECONDS
    # and retries once. If it gets a second 429 (very unlikely), it logs and skips.
    # The script is safe to re-run — any skipped activities will be picked up next time.
    access_token = await strava_service.get_valid_token(telegram_user_id)
    sem = asyncio.Semaphore(CONCURRENCY)
    saved_count = 0
    saved_lock = asyncio.Lock()

    async def fetch_and_save(ride: StravaActivitySummary) -> None:
        nonlocal saved_count
        async with sem:
            for attempt in range(2):  # one retry on 429
                try:
                    streams = await strava_service.get_activity_streams(
                        ride.id, access_token
                    )
                    if not streams:
                        print(f"  [no streams] {ride.name}")
                        return

                    computed = metrics_module.compute_activity_metrics(streams, ftp=FTP)
                    await supabase_service.save_activity_metrics(
                        activity_id=ride.id,
                        telegram_user_id=telegram_user_id,
                        streams=streams,
                        metrics=computed,
                    )
                    async with saved_lock:
                        saved_count += 1
                        print(
                            f"  [saved {saved_count:>3}/{len(cache_misses)}] {ride.name}"
                        )
                    return

                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and attempt == 0:
                        # Strava rate limit hit. Wait for the window to reset, then retry.
                        wait = RETRY_WAIT_SECONDS
                        print(
                            f"\n  [rate limited] Waiting {wait}s before retrying "
                            f"(~{wait // 60} min)... Started at {time.strftime('%H:%M:%S')}\n"
                        )
                        await asyncio.sleep(wait)
                        continue  # retry
                    print(
                        f"  [error]      {ride.name} "
                        f"(HTTP {e.response.status_code})"
                    )
                    return

                except Exception as e:
                    print(f"  [error]      {ride.name}: {e}")
                    return

    await asyncio.gather(*[fetch_and_save(ride) for ride in cache_misses])

    print(f"\nDone. {saved_count} of {len(cache_misses)} activities saved.")


if __name__ == "__main__":
    asyncio.run(main())
