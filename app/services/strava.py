"""
app/services/strava.py — Strava API integration.

Responsibilities:
  1. Build the OAuth2 authorization URL to send the user to Strava's consent page
  2. Exchange a one-time authorization code for access/refresh tokens (Step 4 of OAuth)
  3. Refresh an expired access_token using the stored refresh_token
  4. Provide get_valid_token() — a transparent helper that checks expiry and
     refreshes automatically, so callers never worry about token lifecycle
  5. Fetch recent activity summaries from the Strava v3 API

Design: lazy httpx AsyncClient singleton
  We use the same lazy singleton pattern as supabase.py: a module-level
  variable initialized to None, created on first use. The difference here is
  that httpx.AsyncClient.__init__ is a regular synchronous constructor, so
  _get_http_client() does NOT need to be async — unlike Supabase's
  acreate_client() which is a coroutine.

  Reusing a single AsyncClient across requests is important for efficiency:
  the client maintains a connection pool. Creating a new client per request
  would open and tear down a TCP connection every time.

Data flow (this session):
  Strava API → services/strava.py (fetch token, fetch activities)
             → services/supabase.py (persist/refresh tokens)

Next session (Session 6):
  services/strava.py → services/coach.py (compute NP, VI, zone times, etc.)
                     → services/claude.py (inject metrics into prompt)
"""

import time

import httpx

from app.config import settings
from app.models.schemas import StravaTokenResponse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE  = "https://www.strava.com/api/v3"
STRAVA_AUTH_BASE = "https://www.strava.com/oauth/authorize"

# ---------------------------------------------------------------------------
# HTTP client singleton
# ---------------------------------------------------------------------------

# Module-level client handle. None until the first call to _get_http_client().
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """
    Return the shared httpx AsyncClient, creating it on the first call.

    Note: this function is NOT async, unlike _get_client() in supabase.py.
    httpx.AsyncClient() is a regular synchronous constructor — you don't
    await it. Only the methods (.get(), .post(), etc.) are coroutines.

    The `global` keyword is needed because we're reassigning the module-level
    variable, not just reading it. Without it, Python would create a local
    variable with the same name and leave the module-level one as None.
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient()
    return _http_client


# ---------------------------------------------------------------------------
# Step 1 of OAuth: build the authorization URL
# ---------------------------------------------------------------------------

def generate_auth_url(telegram_user_id: int) -> str:
    """
    Build the Strava OAuth2 authorization URL to send to the user.

    The user opens this URL in a browser and sees Strava's consent page.
    After clicking "Authorize," Strava redirects them to our
    /strava/callback endpoint with a one-time `code` in the URL.

    We encode the `telegram_user_id` in the `state` parameter. This serves
    two purposes:
      1. CSRF protection: the state we set must come back unchanged.
         If state is missing or different, the callback rejects the request.
      2. User identification: the callback knows which Telegram user to link
         the tokens to, without needing a session cookie or database lookup.

    Args:
        telegram_user_id: The Telegram user initiating the connection.

    Returns:
        A full authorization URL string the user opens in their browser.

    Scopes requested:
      - read: basic athlete profile (name, city, sport)
      - activity:read_all: all activities including private ones
    """
    params = {
        "client_id": settings.STRAVA_CLIENT_ID,
        "redirect_uri": settings.STRAVA_REDIRECT_URI,
        "response_type": "code",
        # "auto" skips the consent screen if the user has already authorized
        # this app before. "force" always shows the consent screen.
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
        # Encode user_id as a plain string integer. The callback parses it
        # back with int(state). Keep it simple — no signing needed for a
        # personal single-user bot, but the CSRF check still applies.
        "state": str(telegram_user_id),
    }
    # httpx.URL builds a properly percent-encoded URL from a base + params dict.
    # This handles any special characters in values correctly — safer than
    # manually concatenating query strings with f-strings.
    url = httpx.URL(STRAVA_AUTH_BASE, params=params)
    return str(url)


# ---------------------------------------------------------------------------
# Step 4 of OAuth: exchange the code for tokens
# ---------------------------------------------------------------------------

async def exchange_code_for_tokens(code: str) -> StravaTokenResponse:
    """
    Exchange a one-time authorization code for a set of OAuth2 tokens.

    This is the back-channel step (Step 4 of the dance): the browser received
    a `code` in the redirect URL, our server extracts it, and we POST it to
    Strava's token endpoint server-to-server. The actual tokens never appear
    in a browser URL.

    Args:
        code: The short-lived code from Strava's redirect. Codes are
            single-use and expire within a few minutes — exchange immediately.

    Returns:
        A StravaTokenResponse containing:
          - access_token: use for all subsequent API calls
          - refresh_token: use to get a new access_token when this one expires
          - expires_at: Unix timestamp when access_token expires
          - athlete: the user's Strava profile (only present on this initial exchange)

    Raises:
        httpx.HTTPStatusError: if Strava returns 4xx or 5xx.
          Common causes: code already used or expired, wrong client_id/secret,
          redirect_uri doesn't match what was registered in Strava API settings.
    """
    client = _get_http_client()
    response = await client.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        },
    )
    # raise_for_status() raises httpx.HTTPStatusError if status >= 400.
    # This converts a silent failure (we got a response but it was an error)
    # into a Python exception the caller can catch and handle.
    response.raise_for_status()
    return StravaTokenResponse(**response.json())


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

async def refresh_access_token(refresh_token: str) -> StravaTokenResponse:
    """
    Use a refresh_token to get a new access_token from Strava.

    Strava access_tokens expire after ~6 hours. When one expires, call this
    function with the stored refresh_token to get a fresh pair.

    Important: Strava *rotates* the refresh_token on every refresh call.
    The response includes a NEW refresh_token that replaces the old one.
    You must save both the new access_token AND the new refresh_token, or
    the next refresh will fail with an invalid token error.

    Args:
        refresh_token: The long-lived refresh token stored in strava_tokens.

    Returns:
        A StravaTokenResponse with fresh token values. Note: `athlete` is
        None here — Strava only includes the athlete profile on the initial
        code exchange, not on refreshes.

    Raises:
        httpx.HTTPStatusError: if Strava returns 4xx/5xx.
          A 401 means the refresh token was revoked — the user de-authorized
          your app in their Strava account settings. At that point you must
          re-run the full OAuth dance to get new tokens.
    """
    client = _get_http_client()
    response = await client.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": settings.STRAVA_CLIENT_ID,
            "client_secret": settings.STRAVA_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    response.raise_for_status()
    return StravaTokenResponse(**response.json())


# ---------------------------------------------------------------------------
# Transparent token lifecycle management
# ---------------------------------------------------------------------------

async def get_valid_token(telegram_user_id: int) -> str:
    """
    Return a guaranteed-valid Strava access_token for the given user.

    This is the only function that activity-fetching code should call for
    credentials. It hides the token refresh lifecycle from callers:

      1. Load stored tokens from the database.
      2. Check if the access_token is still valid (expires_at > now + buffer).
      3. If valid: return it directly.
      4. If expired: refresh, save new tokens to DB, return the new token.

    The 60-second buffer prevents a race condition where the token is valid
    when we check but expires before the HTTP request reaches Strava's servers.

    Args:
        telegram_user_id: Used to look up tokens in the strava_tokens table.

    Returns:
        A valid access_token string ready for use in Authorization headers.

    Raises:
        ValueError: if no tokens exist for this user — they haven't completed
            the OAuth flow yet. The caller should prompt them to authorize.
        httpx.HTTPStatusError: if the refresh call fails (e.g. token revoked).
    """
    # Import here instead of at the top of the file to avoid a circular import.
    # strava.py → supabase.py is fine (no cycle), but keeping the import local
    # signals clearly that this function is where the inter-service dependency
    # lives, making it easy to find if the import ever needs to change.
    from app.services import supabase as supabase_service

    token_record = await supabase_service.get_strava_tokens(telegram_user_id)
    if token_record is None:
        raise ValueError(
            f"No Strava tokens found for telegram_user_id={telegram_user_id}. "
            "The user must complete the Strava OAuth authorization flow first."
        )

    # Check expiry with a 60-second buffer.
    # time.time() returns seconds since epoch as a float — same unit as expires_at.
    buffer_seconds = 60
    if token_record.expires_at > (time.time() + buffer_seconds):
        # Token is still valid; return it directly.
        return token_record.access_token

    # Token is expired (or within the buffer window). Refresh it.
    print(f"Strava access token expired for user {telegram_user_id}, refreshing...")
    new_tokens = await refresh_access_token(token_record.refresh_token)

    # Save BOTH the new access_token AND the new refresh_token.
    # Strava rotates refresh tokens — if we only saved access_token here,
    # the stored refresh_token would become stale and the next refresh would fail.
    await supabase_service.update_strava_tokens(
        telegram_user_id=telegram_user_id,
        access_token=new_tokens.access_token,
        refresh_token=new_tokens.refresh_token,
        expires_at=new_tokens.expires_at,
    )

    return new_tokens.access_token


# ---------------------------------------------------------------------------
# Activity fetching
# ---------------------------------------------------------------------------

async def get_recent_activities(
    telegram_user_id: int,
    per_page: int = 10,
) -> list[dict]:
    """
    Fetch the most recent activity summaries for the authorized user.

    Uses the GET /athlete/activities endpoint, which returns activity summary
    objects — not full detail with streams. Streams (raw per-second power/HR
    data for NP, VI, zone time computation) come in a later session.

    Args:
        telegram_user_id: Used by get_valid_token() to retrieve credentials.
        per_page: Number of activities to return. Strava's maximum is 200.

    Returns:
        A list of activity summary dicts as Strava returns them.
        Relevant fields include:
          - id (int): Strava's activity ID — needed for stream fetches later
          - name (str): Activity title, e.g. "Morning Ride"
          - type (str): "Ride", "Run", "VirtualRide", etc.
          - start_date (str): ISO 8601 datetime in UTC
          - distance (float): meters
          - moving_time (int): seconds
          - total_elevation_gain (float): meters
          - average_watts (float | None): only for power meter activities
          - weighted_average_watts (float | None): Strava's NP estimate (premium)
          - average_heartrate (float | None): if heart rate data exists

        We return raw dicts rather than a Pydantic model because the Strava
        activity summary shape has ~50 fields and we don't yet know which ones
        coach.py will need. In a later session, define a StravaActivitySummary
        model for the specific subset we use.

    Raises:
        ValueError: if no tokens exist (user hasn't authorized).
        httpx.HTTPStatusError: on Strava API error (rate limit, bad token, etc.)
            Strava's rate limits: 100 requests/15 minutes, 1000/day.
    """
    access_token = await get_valid_token(telegram_user_id)
    client = _get_http_client()

    response = await client.get(
        f"{STRAVA_API_BASE}/athlete/activities",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"per_page": per_page},
    )
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Stream fetching
# ---------------------------------------------------------------------------

async def get_activity_streams(
    activity_id: int,
    access_token: str,
) -> dict[str, list]:
    """
    Fetch per-second stream data for a single activity.

    Requests four stream types in a single API call:
      - watts:        per-second power in watts (absent if no power meter)
      - heartrate:    per-second HR in bpm (absent if no HR monitor)
      - time:         seconds elapsed from start (always present for GPS activities)
      - grade_smooth: smoothed gradient % — used for climb detection

    All four are requested together; Strava only returns the ones it has data for.
    The response with key_by_type=true looks like:
      {"watts": {"data": [250, 248, ...], ...}, "heartrate": {...}, ...}

    We strip the metadata wrapper and return just the data arrays:
      {"watts": [250, 248, ...], "heartrate": [...], ...}

    Args:
        activity_id: Strava's numeric activity ID (from the activities list).
        access_token: A valid Strava bearer token. The caller is responsible
            for obtaining a valid token (via get_valid_token()) before calling
            this function. Taking the token directly rather than telegram_user_id
            lets coach.py call get_valid_token() once and reuse it for multiple
            stream fetches, avoiding repeated database reads.

    Returns:
        Dict of stream arrays keyed by type. May be empty ({}) for manual
        entries or activities recorded without GPS.

    Raises:
        httpx.HTTPStatusError: on non-404 HTTP errors (rate limit, bad token, etc.)
            404 is treated as "no streams available" and returns {} instead.
    """
    client = _get_http_client()
    response = await client.get(
        f"{STRAVA_API_BASE}/activities/{activity_id}/streams",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "keys": "watts,heartrate,time,grade_smooth",
            # key_by_type=true returns a dict keyed by stream name instead of
            # a flat list of stream objects — much easier to work with.
            "key_by_type": "true",
        },
    )

    # 404 means this activity has no streams (manual entry, or Strava hasn't
    # processed it yet). Return empty dict so the caller can skip metrics
    # computation gracefully rather than crashing.
    if response.status_code == 404:
        return {}

    response.raise_for_status()

    # The API returns {"stream_type": {"data": [...], "series_type": ..., ...}, ...}
    # We only need the "data" array from each stream.
    raw = response.json()
    return {stream_type: stream_obj["data"] for stream_type, stream_obj in raw.items()}


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

async def close() -> None:
    """
    Close the httpx client's connection pool.

    Call from FastAPI's lifespan shutdown handler in main.py, alongside
    the Supabase client close() call. Without this, Python logs warnings
    about unclosed asyncio connections when the process exits.
    """
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
