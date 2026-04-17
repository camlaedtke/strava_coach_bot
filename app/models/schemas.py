"""
app/models/schemas.py — Pydantic models for external API payloads.

These models describe the shape of data coming in from Telegram (and later
Strava). Pydantic validates the incoming JSON against the model; if a required
field is missing or the wrong type, it raises a 422 before our handler runs.

Telegram's Update object is documented at:
https://core.telegram.org/bots/api#update

We only model the fields we actually use. Unknown fields are ignored by
default, so Telegram adding new fields to the payload won't break us.
"""

from pydantic import BaseModel, Field


class User(BaseModel):
    """
    Represents the Telegram user who sent a message.
    In the raw JSON this object is at update.message.from — but "from"
    is a reserved keyword in Python, so we rename it to from_user using
    a Field alias (see Message below).
    """

    id: int
    first_name: str
    username: str | None = None  # Not all users have a username set


class Chat(BaseModel):
    """
    The chat a message was sent in. For direct messages to the bot,
    chat.id equals the sender's user ID. We send replies to this ID.
    """

    id: int
    # "private", "group", "supergroup", or "channel"
    type: str


class Message(BaseModel):
    """
    A single message received by the bot.

    The trickiest part: Telegram's JSON has a key called "from" (the sender),
    but Python won't let us write `from: User` because "from" is a keyword.

    Solution: declare the field as `from_user` and attach
    `Field(alias="from")`. Pydantic will look for "from" in the incoming
    JSON and store it as `from_user` in Python. The `populate_by_name=True`
    in model_config lets us also set it by the Python name in tests/code.
    """

    model_config = {"populate_by_name": True}

    message_id: int
    text: str | None = None  # None for stickers, photos, etc.
    chat: Chat
    from_user: User | None = Field(default=None, alias="from")


class Update(BaseModel):
    """
    The top-level object Telegram POSTs to our webhook.

    Every update has an update_id (sequential integer) and exactly one
    of many possible payload types: message, edited_message, callback_query,
    etc. We only handle `message` for now.
    """

    update_id: int
    message: Message | None = None


class ConversationMessage(BaseModel):
    """
    A single message turn as stored in our database and passed to Claude.

    NOT the same as Telegram's Message model above — this represents a row
    from our own `messages` table. We use a distinct name to avoid confusion.

    The `role` field matches the Anthropic API convention: "user" for the
    human's turns, "assistant" for Claude's replies. When building the
    messages= list for the Claude API, we call .model_dump() on each of these
    to get the {"role": ..., "content": ...} dict the SDK expects.
    """

    role: str    # "user" or "assistant"
    content: str


class UserRecord(BaseModel):
    """
    Represents a row from our `users` table, returned after an upsert.

    The telegram router needs the internal `id` to save and load messages
    (the messages table foreign-keys to users.id, not telegram_user_id).
    Returning a typed model here rather than a raw dict makes that dependency
    explicit and gives us IDE autocomplete on the fields.
    """

    id: int
    telegram_user_id: int
    first_name: str
    username: str | None = None


class StravaCallbackParams(BaseModel):
    """
    Query parameters Strava sends to our /strava/callback endpoint.

    After the user authorizes on Strava's consent page, Strava redirects to:
      https://our-domain/strava/callback?code=<code>&state=<state>&scope=<scope>

    The `code` is a one-time authorization code — short-lived (minutes) and
    single-use. We exchange it immediately for long-lived tokens in Step 4 of
    the OAuth dance.

    Note: if the user *denies* access, Strava sends ?error=access_denied instead
    of ?code=... The router handles that case separately by checking for `error`.
    """

    code: str        # One-time authorization code to exchange for tokens
    state: str       # Echo of the state we sent; we parse telegram_user_id from it
    scope: str = ""  # Comma-separated granted scopes, e.g. "read,activity:read_all"


class StravaTokenResponse(BaseModel):
    """
    The JSON body Strava returns when we POST to their token endpoint.

    This shape is returned for BOTH the initial code exchange and for token
    refreshes. The one difference: `athlete` is only present on the initial
    exchange — it's None on refresh responses.

    Strava docs: https://developers.strava.com/docs/authentication/
    """

    token_type: str       # Always "Bearer"
    access_token: str     # Short-lived; send as "Authorization: Bearer <token>"
    refresh_token: str    # Long-lived; use to get a new access_token when expired
    expires_at: int       # Unix timestamp (seconds) when access_token expires (~6 hrs)
    expires_in: int       # Seconds until expiry — redundant with expires_at, included for completeness
    athlete: dict | None = None  # Strava athlete profile; only present on initial exchange


class StravaTokenRecord(BaseModel):
    """
    Represents a row from our `strava_tokens` table.

    Returned by get_strava_tokens() so callers get typed field access instead
    of working with raw dicts. The expires_at field is an integer (Unix
    timestamp in seconds) for direct comparison with time.time().
    """

    id: int
    telegram_user_id: int
    access_token: str
    refresh_token: str
    expires_at: int
    strava_athlete_id: int | None = None


class StravaActivitySummary(BaseModel):
    """
    The subset of Strava's activity summary object that coach.py uses.

    Strava's full summary has ~50 fields; we model only the ones we actually
    read so the shape is explicit and callers get typed access instead of
    raw dict lookups. Unknown fields are ignored by default (Pydantic's
    behavior with extra='ignore').

    Units are as Strava returns them — coach.py converts to imperial for display:
      distance: meters → miles
      total_elevation_gain: meters → feet
      moving_time: seconds → H:MM

    average_watts and weighted_average_watts are only present for power-meter
    activities; both are None for rides without a power meter.
    weighted_average_watts is Strava's Normalized Power estimate — only available
    with Strava Premium ("Summit"). It's a useful proxy until we compute real NP
    from per-second streams.
    """

    model_config = {"extra": "ignore"}  # silently drop the other ~40 fields

    id: int
    name: str
    type: str                                    # "Ride", "VirtualRide", "Run", etc.
    start_date: str                              # ISO 8601 UTC, e.g. "2026-04-15T10:30:00Z"
    distance: float                              # meters
    moving_time: int                             # seconds
    total_elevation_gain: float                  # meters
    average_watts: float | None = None
    weighted_average_watts: int | None = None    # Strava's NP estimate (Premium only)
    average_heartrate: float | None = None
    max_heartrate: float | None = None
