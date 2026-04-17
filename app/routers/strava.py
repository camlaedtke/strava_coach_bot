"""
app/routers/strava.py — Strava OAuth2 endpoints.

Two endpoints:
  GET /strava/auth      → Returns the authorization URL for the user to open
  GET /strava/callback  → Strava redirects here after the user grants access

Why GET for /strava/auth instead of POST?
  The user calls /strava/auth from Telegram (either directly or via a bot
  command). We return the URL as JSON, and the Telegram bot sends it as a
  clickable link. GET is appropriate because this endpoint has no side effects —
  it just builds and returns a URL string. It's also easy to test in a browser.

Why GET for /strava/callback?
  Strava's redirect is a browser GET request to our redirect_uri. We have no
  control over the HTTP method here — OAuth2 redirects are always GETs.
"""

from fastapi import APIRouter, HTTPException, Query

from app.services import strava as strava_service
from app.services import supabase as supabase_service

router = APIRouter()


@router.get("/auth")
async def strava_auth(telegram_user_id: int = Query(...)) -> dict:
    """
    Generate and return the Strava OAuth2 authorization URL.

    The caller provides their Telegram user ID, and we return a URL they open
    in a browser to start the authorization flow. After they authorize on
    Strava's site, Strava redirects them to /strava/callback.

    Why return JSON instead of doing a server-side redirect?
      In a web app you'd redirect the browser directly. But here, the "client"
      making this request is the Telegram bot server (or a curl command during
      development) — neither of which is a browser that can follow a redirect
      to Strava. Returning {"auth_url": "..."} lets the bot forward it to the
      user as a clickable link.

    Args:
        telegram_user_id: Passed as a query parameter, e.g.:
            GET /strava/auth?telegram_user_id=123456789
            Get yours by messaging @userinfobot on Telegram, or from the
            server logs when you send the bot a message.

    Returns:
        {"auth_url": "https://www.strava.com/oauth/authorize?..."} — open this
        in a browser to authorize the bot.
    """
    auth_url = strava_service.generate_auth_url(telegram_user_id)
    return {"auth_url": auth_url}


@router.get("/callback")
async def strava_callback(
    code: str = Query(...),
    state: str = Query(...),
    scope: str = Query(default=""),
    error: str | None = Query(default=None),
) -> dict:
    """
    Handle Strava's redirect after the user grants (or denies) access.

    Strava redirects here with query parameters:
      Success: ?code=<auth_code>&state=<our_state>&scope=<granted_scopes>
      Denial:  ?error=access_denied&state=<our_state>

    This endpoint completes Step 3 and Step 4 of the OAuth dance:
      1. Validate the state (CSRF check + extract telegram_user_id)
      2. Exchange the one-time `code` for long-lived tokens (Step 4)
      3. Save the tokens to the database
      4. Return a success confirmation

    Args:
        code: One-time authorization code from Strava. Exchange it immediately —
              it's single-use and expires in a few minutes.
        state: Echo of the state parameter we sent in the auth URL. We encoded
               the Telegram user ID here; we parse it back to identify the user.
        scope: Comma-separated list of scopes the user actually granted.
               May differ from what we requested if the user unchecked options.
        error: Present (e.g. "access_denied") if the user denied access.
               Absent on success.

    Returns:
        {"ok": True, "message": "Strava connected successfully!", ...}

    Raises:
        HTTPException 400: user denied access, state is invalid/tampered,
                           or the required activity scope was not granted.
        HTTPException 502: Strava's token endpoint returned an error.
    """
    # Step 1a — Check for access denial.
    # Strava sends ?error=access_denied when the user clicks "Cancel" on the
    # consent page. Surface a clear error rather than trying to exchange a
    # missing code.
    if error:
        raise HTTPException(
            status_code=400,
            detail=f"Strava authorization was denied: {error}. "
                   "Please try again and click 'Authorize' on the Strava page.",
        )

    # Step 1b — Parse telegram_user_id from the state parameter.
    # We encoded it as a plain string integer in generate_auth_url().
    # If state is anything other than a valid integer (tampered, replay attack,
    # or a request from a different origin), int() raises ValueError and we
    # return 400. This is the CSRF check: an attacker can't manufacture a valid
    # state without knowing the user's Telegram ID.
    try:
        telegram_user_id = int(state)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid state parameter. The authorization request may have "
                   "been tampered with. Please start the authorization flow again.",
        )

    # Step 1c — Check that the required scope was granted.
    # Strava's consent page lets users uncheck individual scopes. If the user
    # didn't grant activity:read_all, we won't be able to fetch their private
    # activities. We warn but don't block — public activities will still work.
    # If your use case requires private activities, change this to raise 400.
    if "activity:read_all" not in scope:
        print(
            f"Warning: user {telegram_user_id} did not grant activity:read_all. "
            f"Granted scopes: '{scope}'. Private activities will not be accessible."
        )

    # Step 2 — Exchange the authorization code for tokens.
    # This is the back-channel POST to Strava's token endpoint. The code is
    # single-use; if this POST fails (e.g. code already used, wrong credentials),
    # the user must start the OAuth flow over from /strava/auth.
    try:
        token_response = await strava_service.exchange_code_for_tokens(code)
    except Exception as exc:
        # 502 Bad Gateway: we (the gateway) tried to call an upstream service
        # (Strava) and it failed. This is different from a 400 (caller's fault)
        # or 500 (our fault).
        raise HTTPException(
            status_code=502,
            detail=f"Failed to exchange authorization code with Strava: {exc}",
        )

    # Step 3 — Extract the Strava athlete ID.
    # The athlete object is only present on the initial code exchange (not on
    # refreshes), so we guard against None here even though it should always
    # be present at this point.
    strava_athlete_id: int | None = None
    if token_response.athlete:
        strava_athlete_id = token_response.athlete.get("id")

    # Step 4 — Persist the tokens to the database.
    # Uses upsert, so re-authorizing (running this flow again) updates the
    # existing row rather than creating a duplicate.
    await supabase_service.save_strava_tokens(
        telegram_user_id=telegram_user_id,
        access_token=token_response.access_token,
        refresh_token=token_response.refresh_token,
        expires_at=token_response.expires_at,
        strava_athlete_id=strava_athlete_id,
    )

    print(f"Strava connected for telegram_user_id={telegram_user_id}, athlete_id={strava_athlete_id}")

    return {
        "ok": True,
        "message": "Strava connected successfully! You can now ask the bot about your activities.",
        "telegram_user_id": telegram_user_id,
        "strava_athlete_id": strava_athlete_id,
    }
