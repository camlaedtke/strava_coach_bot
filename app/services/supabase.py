"""
app/services/supabase.py — Supabase database operations.

This module handles all reads and writes to our Supabase (PostgreSQL) database.
It exposes four public async functions:

  - get_or_create_user()   Upsert a Telegram user, return their internal record
  - save_message()         Insert one message row (user or assistant turn)
  - get_recent_messages()  Fetch the last N messages for use as Claude context
  - close()                Cleanly shut down the HTTP connection pool

Design: lazy async singleton
------------------------------
The Supabase client is stored in a module-level variable `_client` initialized
to None. The private `_get_client()` coroutine creates it on the first call and
returns the same instance on every subsequent call.

Why lazy instead of creating it at import time (like _client in claude.py)?
  The supabase-py v2 async client is created with `acreate_client()`, which is
  itself a coroutine — it must be awaited inside a running event loop. If we
  called it at module import time (before FastAPI starts its event loop), Python
  would raise "no running event loop". The lazy pattern defers that call until
  the first actual request arrives, when the loop is already running.

Why not use the sync client (create_client without the 'a')?
  The sync client blocks the thread while waiting for Supabase's HTTP response.
  In FastAPI's async model that stalls the entire event loop — every other
  request waits while we wait for the database. Even on a low-traffic personal
  bot, this is the wrong habit to build. The async client releases control to
  the event loop while waiting, so other requests can proceed in parallel.

Why not initialize via FastAPI's lifespan event?
  That works too, but requires passing the client through app.state or a
  global — coupling main.py to this module's internals. The lazy singleton is
  fully self-contained: callers just import the functions and call them without
  knowing or caring when initialization happens.
"""

import dataclasses

from supabase import acreate_client, AsyncClient

from app.config import settings
from app.models.schemas import ConversationMessage, UserRecord, StravaTokenRecord
from app.services.metrics import ActivityMetrics, activity_metrics_from_dict

# How many past messages to load as context for Claude.
# 20 messages (~10 back-and-forth exchanges) is roughly 2,000–4,000 tokens of
# history — well within Claude Sonnet's 200k context window. Increase this
# if you want deeper memory; decrease it to reduce cost per request.
HISTORY_LIMIT = 20

# Module-level client handle. None until the first call to _get_client().
_client: AsyncClient | None = None


async def _get_client() -> AsyncClient:
    """
    Return the shared AsyncClient, creating it on the first call.

    The `global` keyword is required here because we're *reassigning* the
    module-level `_client` variable (not just reading or mutating it). Without
    `global`, Python would create a new local variable with the same name and
    leave the module-level one as None.
    """
    global _client
    if _client is None:
        _client = await acreate_client(
            supabase_url=settings.SUPABASE_URL,
            supabase_key=settings.SUPABASE_KEY,
        )
    return _client


async def get_or_create_user(
    telegram_user_id: int,
    first_name: str,
    username: str | None,
) -> UserRecord:
    """
    Upsert a Telegram user and return their database record.

    "Upsert" means INSERT ... ON CONFLICT DO UPDATE. If a row with this
    telegram_user_id already exists, we update first_name and username in
    case the user changed them. If no row exists, we insert one.

    We do this on every incoming message (not just on first contact) because
    Telegram users can change their display name and @username at any time.
    Keeping the row current means our database always reflects who they are.

    The Supabase PostgREST client's .upsert() method maps to:
      INSERT INTO users (...) VALUES (...)
      ON CONFLICT (telegram_user_id) DO UPDATE SET first_name=..., username=...

    Args:
        telegram_user_id: The user.id from Telegram's User object. This is a
            stable integer Telegram assigns — it never changes for a given user.
        first_name: The user's current display name (can change over time).
        username: The @handle, or None if the user hasn't set one.

    Returns:
        A UserRecord with the internal database `id` (our primary key) needed
        to save and load this user's messages.
    """
    client = await _get_client()
    response = (
        await client.table("users")
        .upsert(
            {
                "telegram_user_id": telegram_user_id,
                "first_name": first_name,
                "username": username,
            },
            # on_conflict tells PostgREST which column's uniqueness constraint
            # to use for detecting a conflict. This must match the UNIQUE
            # constraint or index defined in the SQL schema.
            on_conflict="telegram_user_id",
        )
        .execute()
    )
    # .execute() returns a PostgrestResponse. .data is a list of the rows
    # affected by the operation. An upsert of a single row returns a list
    # with exactly one dict. We unpack and validate it with our Pydantic model.
    return UserRecord(**response.data[0])


async def save_message(
    user_id: int,
    role: str,
    content: str,
) -> None:
    """
    Insert a single message row into the messages table.

    Called twice per Telegram interaction: once before calling Claude
    (role="user") and once after (role="assistant"). This ensures both sides
    of the conversation are persisted symmetrically.

    Args:
        user_id: The internal `users.id` primary key — NOT telegram_user_id.
            The messages table foreign-keys to users.id to keep referential
            integrity (if a user row is deleted, their messages go with it).
        role: "user" for the human's turn, "assistant" for Claude's reply.
            Must match the CHECK constraint in the SQL schema.
        content: The raw text of the message.
    """
    client = await _get_client()
    await (
        client.table("messages")
        .insert({"user_id": user_id, "role": role, "content": content})
        .execute()
    )


async def get_recent_messages(
    user_id: int,
    limit: int = HISTORY_LIMIT,
) -> list[ConversationMessage]:
    """
    Fetch the most recent `limit` messages for a user in chronological order.

    This function powers Claude's memory: we load the last N message turns and
    pass them as the `history` argument to get_claude_reply(), so Claude has
    context from earlier in the conversation.

    Query strategy — why we sort DESC then reverse in Python:
      We want the most recent `limit` rows, but the Anthropic API requires the
      messages list to be in chronological order (oldest first). If we sorted
      ASC in the query, we'd get the oldest rows, not the newest ones. Instead:
        1. Sort DESC to get the newest `limit` rows.
        2. Reverse the list in Python to restore chronological order.
      This two-step is a common pattern when you need "last N, oldest-first."

    Args:
        user_id: The internal `users.id` primary key.
        limit: Maximum number of messages to return. Defaults to HISTORY_LIMIT.

    Returns:
        A list of ConversationMessage objects, oldest-first, ready to pass
        directly to get_claude_reply() as the `history` argument.
    """
    client = await _get_client()
    response = (
        await client.table("messages")
        .select("role, content")       # only fetch the columns we need
        .eq("user_id", user_id)        # filter to this user's messages
        .order("created_at", desc=True) # newest first (see query strategy above)
        .limit(limit)
        .execute()
    )
    # response.data is a list of dicts: [{"role": "user", "content": "..."}, ...]
    # Reverse to restore chronological (oldest-first) order before returning.
    rows = list(reversed(response.data))
    return [ConversationMessage(**row) for row in rows]


async def close() -> None:
    """
    Close the Supabase client's underlying HTTP connections.

    Called from FastAPI's lifespan shutdown handler in main.py. Without this,
    Python will log warnings about unclosed httpx connections when the process
    exits. It's a cleanup courtesy — not calling it won't break anything, but
    it keeps the shutdown logs clean.
    """
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Strava token storage
# ---------------------------------------------------------------------------

async def save_strava_tokens(
    telegram_user_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: int,
    strava_athlete_id: int | None = None,
) -> None:
    """
    Insert or update Strava tokens for a Telegram user.

    Uses upsert on the `telegram_user_id` unique constraint so running the
    OAuth flow a second time (re-authorizing) updates the tokens in place
    rather than inserting a duplicate row.

    Args:
        telegram_user_id: The Telegram user who completed the OAuth flow.
        access_token: Short-lived bearer token from Strava.
        refresh_token: Long-lived token used to renew the access_token.
        expires_at: Unix timestamp (seconds) when access_token expires.
        strava_athlete_id: Strava's numeric athlete ID. Only present on the
            initial token exchange (not on refreshes), so it can be None.
    """
    client = await _get_client()
    await (
        client.table("strava_tokens")
        .upsert(
            {
                "telegram_user_id": telegram_user_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "strava_athlete_id": strava_athlete_id,
                # "now()" is a SQL literal that Supabase/PostgREST evaluates
                # server-side. This ensures updated_at reflects the database
                # server's clock rather than our application server's clock.
                "updated_at": "now()",
            },
            on_conflict="telegram_user_id",
        )
        .execute()
    )


async def get_strava_tokens(telegram_user_id: int) -> StravaTokenRecord | None:
    """
    Fetch stored Strava tokens for a Telegram user, or None if not authorized.

    Called by get_valid_token() in services/strava.py before every API call.
    The caller uses the returned expires_at to decide whether to use the token
    directly or refresh it first.

    Args:
        telegram_user_id: The Telegram user to look up.

    Returns:
        A StravaTokenRecord with all token fields, or None if the user has
        not completed the Strava OAuth flow yet.
    """
    client = await _get_client()
    response = (
        await client.table("strava_tokens")
        .select("*")
        .eq("telegram_user_id", telegram_user_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        return None
    return StravaTokenRecord(**response.data[0])


# ---------------------------------------------------------------------------
# Activity metrics cache
# ---------------------------------------------------------------------------
# These functions back the "fetch-or-cache" pattern in coach.py.
# The first time an activity is seen, its streams are fetched from Strava,
# metrics are computed, and both are stored here. On subsequent messages the
# DB row is returned immediately — no Strava API call needed.
#
# Required Supabase table (run once in Supabase SQL editor):
#
#   CREATE TABLE activity_metrics (
#       id               BIGSERIAL PRIMARY KEY,
#       activity_id      BIGINT NOT NULL UNIQUE,
#       telegram_user_id BIGINT NOT NULL,
#       streams          JSONB  NOT NULL,
#       metrics          JSONB  NOT NULL,
#       created_at       TIMESTAMPTZ DEFAULT now()
#   );
#
# activity_id is UNIQUE because Strava activity IDs are globally unique
# (not scoped to a user). Upserting on this column is safe.

async def get_cached_metrics(activity_id: int) -> ActivityMetrics | None:
    """
    Return precomputed ActivityMetrics for an activity if they exist in the DB.

    This is the cache-read half of the fetch-or-cache pattern. On a cache hit,
    coach.py uses these metrics directly without calling the Strava streams API.

    Args:
        activity_id: Strava's numeric activity ID.

    Returns:
        An ActivityMetrics object reconstructed from stored JSONB, or None if
        this activity hasn't been seen before.
    """
    client = await _get_client()
    response = (
        await client.table("activity_metrics")
        .select("metrics")
        .eq("activity_id", activity_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        return None
    # response.data[0]["metrics"] is a dict (Supabase deserializes JSONB for us).
    # activity_metrics_from_dict handles the nested ClimbSegment reconstruction.
    return activity_metrics_from_dict(response.data[0]["metrics"])


async def save_activity_metrics(
    activity_id: int,
    telegram_user_id: int,
    streams: dict,
    metrics: ActivityMetrics,
) -> None:
    """
    Persist raw streams and computed metrics for an activity.

    This is the cache-write half of the fetch-or-cache pattern. Called once
    per activity after fetching streams from Strava and computing metrics.

    Uses upsert on activity_id so re-running the OAuth flow or re-processing
    an activity doesn't create duplicate rows.

    Args:
        activity_id: Strava's numeric activity ID (unique across all athletes).
        telegram_user_id: The Telegram user who owns this activity.
        streams: Raw stream arrays from get_activity_streams(), stored as-is.
            Storing the raw streams allows recomputing metrics if formulas
            change later, without re-fetching from Strava.
        metrics: Computed ActivityMetrics; serialized via dataclasses.asdict().
            The nested ClimbSegment objects are handled correctly by asdict().
    """
    client = await _get_client()
    await (
        client.table("activity_metrics")
        .upsert(
            {
                "activity_id": activity_id,
                "telegram_user_id": telegram_user_id,
                "streams": streams,
                # dataclasses.asdict() recursively converts all nested dataclasses
                # (including ClimbSegment) to plain dicts — JSONB-serializable.
                "metrics": dataclasses.asdict(metrics),
            },
            on_conflict="activity_id",
        )
        .execute()
    )


async def update_strava_tokens(
    telegram_user_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: int,
) -> None:
    """
    Update Strava tokens for an existing row after a token refresh.

    This is intentionally separate from save_strava_tokens() for clarity:
      - save_strava_tokens() handles the initial INSERT (or re-auth UPDATE)
      - update_strava_tokens() is always an UPDATE on an existing row

    Keeping the intent separate makes the calling code in strava.py easier
    to read — it's obvious whether we're doing initial setup or a refresh.

    Args:
        telegram_user_id: Identifies which row to update.
        access_token: Newly issued access token from the refresh call.
        refresh_token: Newly rotated refresh token — Strava issues a new one
            on every refresh, so we must save this or the next refresh fails.
        expires_at: New expiry timestamp, approximately 6 hours from now.
    """
    client = await _get_client()
    await (
        client.table("strava_tokens")
        .update(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "updated_at": "now()",
            }
        )
        .eq("telegram_user_id", telegram_user_id)
        .execute()
    )
