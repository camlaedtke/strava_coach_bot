"""
scripts/backfill_power_prs.py — Populate power_prs from cached stream data.

Reads raw streams from every row in activity_metrics (no Strava API calls needed —
streams are already stored there from the original backfill), computes best power
for all 12 PR durations, and upserts the all-time maxima into the power_prs table.

Run once after creating the power_prs table in Supabase:
    python scripts/backfill_power_prs.py

Point at prod Supabase:
    python scripts/backfill_power_prs.py --env-file .env.prod

Re-running is safe: the final upsert overwrites the existing record with the same data.
"""

# argparse + dotenv MUST run before any app.* import.
# app.config instantiates Settings() at import time, which is when pydantic-settings
# reads the .env file. By that point it's too late to inject new values via argparse.
# Calling load_dotenv(override=True) first puts values into os.environ, which
# pydantic-settings always reads with higher precedence than its own env_file.
import argparse
import sys
from pathlib import Path
from dotenv import load_dotenv

parser = argparse.ArgumentParser(description="Backfill all-time power PRs from cached streams.")
parser.add_argument(
    "--env-file",
    default=".env",
    help="Path to the .env file to load (default: .env)",
)
args = parser.parse_args()
load_dotenv(args.env_file, override=True)

import asyncio  # noqa: E402 — imports below are intentionally after dotenv load

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.config  # noqa: F401, E402

from app.services import supabase as supabase_service
from app.services.metrics import _best_average_power

# Must match coach.py _PR_LABELS and supabase.py upsert_power_prs expectations.
PR_DURATIONS = [
    ("15s",  15),
    ("30s",  30),
    ("1m",   60),
    ("2m",   120),
    ("3m",   180),
    ("5m",   300),
    ("10m",  600),
    ("15m",  900),
    ("20m",  1200),
    ("30m",  1800),
    ("45m",  2700),
    ("60m",  3600),
]

# Stream JSONB rows are large (per-second data for entire rides), so keep
# batches small to stay under Supabase's statement timeout.
PAGE_SIZE = 25


async def main() -> None:
    # Sanity-check: print which Supabase project we're writing to before any writes.
    # Shows only the subdomain (e.g. "abcdefghijkl") — never the key.
    supabase_url = app.config.settings.SUPABASE_URL
    subdomain = supabase_url.split("//")[-1].split(".")[0] if supabase_url else "(not set)"
    print(f"Supabase project: {subdomain}  ({supabase_url})")
    print(f"Env file: {args.env_file}\n")

    # Step 1: Find telegram_user_id (single-user bot — take the only row)
    client = await supabase_service._get_client()
    result = (
        await client.table("strava_tokens")
        .select("telegram_user_id")
        .limit(1)
        .execute()
    )
    if not result.data:
        raise RuntimeError(
            "No rows in strava_tokens. Complete the Strava OAuth flow first (/strava)."
        )
    telegram_user_id = result.data[0]["telegram_user_id"]
    print(f"Using telegram_user_id={telegram_user_id}\n")

    # Step 2: Read all cached streams from activity_metrics (paginated)
    print("Reading cached streams from activity_metrics...")
    all_rows: list[dict] = []
    offset = 0
    while True:
        response = (
            await client.table("activity_metrics")
            .select("activity_id, streams")
            .eq("telegram_user_id", telegram_user_id)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        if not response.data:
            break
        all_rows.extend(response.data)
        if len(response.data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    print(f"Found {len(all_rows)} cached activities.\n")

    # Step 3: Compute all-time bests across all activities
    all_time: dict[str, float] = {}
    skipped = 0
    for row in all_rows:
        watts_raw = row["streams"].get("watts", [])
        if not watts_raw:
            skipped += 1
            continue
        watts = [float(w) for w in watts_raw]
        for label, secs in PR_DURATIONS:
            best = _best_average_power(watts, secs)
            if best is not None:
                if label not in all_time or best > all_time[label]:
                    all_time[label] = round(best, 1)

    if skipped:
        print(f"  ({skipped} activities had no power stream — skipped)\n")

    if not all_time:
        print("No power data found in cached streams. Nothing to save.")
        return

    # Step 4: Print results and save
    print("All-time power records:")
    for label, _ in PR_DURATIONS:
        val = all_time.get(label)
        if val is not None:
            print(f"  {label:>4}: {val:.0f}W")
        else:
            print(f"  {label:>4}: N/A (all rides shorter than this window)")

    await client.table("power_prs").upsert(
        {
            "telegram_user_id": telegram_user_id,
            "records": all_time,
            "updated_at": "now()",
        },
        on_conflict="telegram_user_id",
    ).execute()
    print("\nSaved to power_prs table.")


if __name__ == "__main__":
    asyncio.run(main())
