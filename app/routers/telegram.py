"""
app/routers/telegram.py — Telegram webhook endpoints.

This module handles two jobs:
  1. Receiving messages from Telegram (POST /telegram/webhook)
  2. Registering our webhook URL with Telegram (POST /telegram/set-webhook)

How the webhook flow works:
  - We tell Telegram "send updates to https://our-domain/telegram/webhook"
  - Whenever a user messages our bot, Telegram POSTs a JSON Update to that URL
  - We parse it, do something (for now: echo it back), and return 200
  - If Telegram doesn't get a 200 within a few seconds, it retries — so we
    must respond quickly and do any slow work asynchronously

About python-telegram-bot's Bot class:
  - Bot is the low-level HTTP client for the Telegram Bot API
  - We use it only for *sending* — it wraps calls like sendMessage, setWebhook
  - We intentionally avoid Bot's higher-level Application/Updater classes
    because those manage their own event loop, which conflicts with FastAPI's
"""

from telegram import Bot

from fastapi import APIRouter, Depends

from app.config import settings
from app.models.schemas import Update
from app.services.coach import get_coaching_reply
from app.services import supabase as supabase_service
from app.services import strava as strava_service

router = APIRouter()


# ---------------------------------------------------------------------------
# Bot command handlers
# ---------------------------------------------------------------------------

async def _handle_command(text: str, telegram_user_id: int) -> str | None:
    """
    Route a Telegram bot command and return a reply string, or None if unknown.

    Called before the coach pipeline whenever the user sends a message starting
    with '/'. Returning None means the command isn't recognized — the message
    falls through to the coach service as a regular coaching question.

    Command responses are intentionally NOT saved to the messages table.
    We don't want Claude seeing "/strava" or an OAuth URL in its conversation
    context on future turns.

    Supported commands:
      /strava  — Show Strava connection status, or send the OAuth authorization URL.
    """
    # Strip the command name from potential "@botname" suffix that Telegram
    # appends in group chats (e.g. "/strava@MyCoachBot").
    command = text.split("@")[0].split()[0].lower()

    if command == "/strava":
        token_record = await supabase_service.get_strava_tokens(telegram_user_id)
        if token_record is not None:
            return (
                "Strava is connected. Your training data is included with every "
                "coaching message."
            )
        else:
            auth_url = strava_service.generate_auth_url(telegram_user_id)
            return (
                "Your Strava account isn't connected yet.\n\n"
                "Open this link to authorize access:\n"
                f"{auth_url}\n\n"
                "Once connected, your recent training data will be included "
                "with every coaching message."
            )

    # Unknown command — return None so the webhook handler falls through
    # to the coach service.
    return None


# --- Dependency: Bot instance ---
#
# FastAPI's dependency injection (Depends) lets us declare that an endpoint
# needs something and have FastAPI build/provide it automatically.
#
# Here we create a fresh Bot object for each request. Bot is lightweight
# (just stores the token and an httpx client) so per-request construction
# is fine. An alternative would be a module-level singleton, but that
# makes testing harder — you'd have to patch a global object.
async def get_bot() -> Bot:
    """Create a Telegram Bot instance using the configured token."""
    return Bot(token=settings.TELEGRAM_BOT_TOKEN)


@router.post("/webhook")
async def telegram_webhook(
    update: Update,
    bot: Bot = Depends(get_bot),
) -> dict:
    """
    Receive a Telegram Update, persist it, call Claude with history, and reply.

    FastAPI automatically:
      - Reads the request body as JSON
      - Validates it against our Update Pydantic model
      - Passes the parsed object to this function
      - Returns our dict as a JSON response with status 200

    Telegram's contract:
      - It expects a 200 response. Anything else triggers a retry.
      - It doesn't care about the response body, but returning {"ok": True}
        is a readable convention.

    Full operation sequence (with Supabase):
      1. Guard: skip non-text updates (stickers, photos, channel posts, etc.)
      2. Upsert the user → get our internal user_id
      3. Save the incoming user message to the database
      4. Load recent conversation history from the database
      5. Pass prior history + current message to Claude
      6. Save Claude's reply to the database
      7. Send Claude's reply back to Telegram
    """
    # Step 1 — Guard: skip updates that aren't text messages from a real user.
    # from_user is None for channel posts (channels don't have a user sender).
    if (
        update.message is None
        or update.message.text is None
        or update.message.from_user is None
    ):
        return {"ok": True}

    chat_id = update.message.chat.id
    user_text = update.message.text
    from_user = update.message.from_user

    # Command dispatch — handle /commands before the coaching pipeline.
    #
    # Bot commands start with '/'. We check here so commands bypass the full
    # coach pipeline (no Strava fetch, no Claude call, no DB history writes).
    # _handle_command returns a reply string for known commands, or None for
    # unknown ones, which then fall through to the coach service.
    if user_text.startswith("/"):
        command_reply = await _handle_command(user_text, from_user.id)
        if command_reply is not None:
            await bot.send_message(chat_id=chat_id, text=command_reply)
            return {"ok": True}
        # Unknown command — fall through to the coach pipeline so the user
        # gets a coaching response rather than silence.

    # Step 2 — Upsert user.
    # We call this on every message so that name/username changes are captured.
    # It returns a UserRecord with our internal `id` (the messages table
    # references this id, not Telegram's user id).
    user_record = await supabase_service.get_or_create_user(
        telegram_user_id=from_user.id,
        first_name=from_user.first_name,
        username=from_user.username,
    )

    # Step 3 — Save the incoming message before calling Claude.
    # Saving first means the full conversation (including this turn) is always
    # in the database, even if the Claude call or Telegram send later fails.
    await supabase_service.save_message(
        user_id=user_record.id,
        role="user",
        content=user_text,
    )

    # Step 4 — Load recent conversation history.
    # get_recent_messages returns up to HISTORY_LIMIT messages, including the
    # one we just saved. We slice off the last item ([:-1]) because
    # get_claude_reply always appends user_text as the final "user" turn itself.
    # Passing it twice would cause Claude to see a duplicate message.
    all_recent = await supabase_service.get_recent_messages(user_id=user_record.id)
    prior_history = all_recent[:-1]  # everything except the message we just saved

    # Step 5 — Call the coach service.
    # coach.get_coaching_reply fetches Strava activities, builds a grounded
    # system prompt (athlete profile + recent training data), and calls Claude.
    # It requires telegram_user_id to look up the user's Strava tokens.
    reply = await get_coaching_reply(
        telegram_user_id=from_user.id,
        user_message=user_text,
        history=prior_history,
    )

    # Step 6 — Save Claude's reply so it becomes part of future context.
    await supabase_service.save_message(
        user_id=user_record.id,
        role="assistant",
        content=reply,
    )

    # Step 7 — Send reply back to Telegram.
    await bot.send_message(chat_id=chat_id, text=reply)

    return {"ok": True}


@router.post("/set-webhook")
async def set_webhook(
    url: str,
    bot: Bot = Depends(get_bot),
) -> dict:
    """
    Register our webhook URL with Telegram.

    This is a dev-only utility endpoint. Call it once after starting the
    server and pointing ngrok at it. Telegram will start POSTing updates
    to the URL you provide.

    Usage:
      curl -X POST "http://localhost:8000/telegram/set-webhook?url=https://abc123.ngrok.io/telegram/webhook"

    The `url` query parameter is FastAPI's way of reading a URL query string.
    Any function parameter that isn't a path variable and isn't typed as a
    Pydantic model is automatically treated as a query parameter.
    """
    result = await bot.set_webhook(url=url)
    return {"ok": result, "webhook_url": url}
