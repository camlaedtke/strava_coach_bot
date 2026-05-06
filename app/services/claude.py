"""
app/services/claude.py — Anthropic Claude API integration.

This module exposes a single public function, get_claude_reply(), that takes
a user's text message and returns Claude's response as a plain string.

Design decisions:
  - The AsyncAnthropic client is a module-level singleton so the underlying
    HTTP connection pool is reused across requests (see _client below).
  - The system prompt is static per request, making it a perfect candidate
    for prompt caching (cache_control annotation).
  - No conversation history is tracked — every message is stateless for now.
    Future: store per-user message history in Supabase and pass it via the
    messages= list so Claude has context across turns.
"""

import anthropic
from anthropic import AsyncAnthropic

from app.config import settings
from app.models.schemas import ConversationMessage

# --- Model constant ---
# Defined once here so changing the model only requires one edit in this file.
CLAUDE_MODEL = "claude-opus-4-7"

# --- Module-level client singleton ---
# AsyncAnthropic manages an httpx connection pool internally. Creating it once
# here means every call to get_claude_reply() reuses those pooled connections
# instead of opening a new TCP connection on each request.
#
# This mirrors the existing pattern in config.py: `settings = Settings()` is
# also a module-level singleton rather than something constructed per-request.
#
# The api_key is read from settings, which reads it from the environment /
# .env file. If ANTHROPIC_API_KEY is missing or empty at startup, this line
# still succeeds (the SDK stores None), but the first API call will raise
# anthropic.AuthenticationError — which is caught below.
_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


async def get_claude_reply(
    user_message: str,
    history: list[ConversationMessage] | None = None,
    system_prompt: str | None = None,
) -> str:
    """
    Send a message to Claude and return its reply as a plain string.

    This is the single entry point for all Claude interactions in this service.
    The Telegram router calls this function and passes the result directly to
    Telegram's send_message.

    Args:
        user_message: The raw text the Telegram user sent to the bot.
        history: Prior conversation turns loaded from Supabase, oldest-first.
            Each item is a ConversationMessage with role ("user"/"assistant")
            and content. If None or empty, behaves like the original stateless
            version — just the single current message is sent to Claude.
        system_prompt: The system prompt to use. Built by coach.py with the
            athlete profile + recent Strava data injected. If None, Claude
            receives an empty system prompt and replies without a persona.

    Returns:
        Claude's reply as a plain string, ready to send back to Telegram.
        On API error, returns a safe fallback string instead of raising —
        this prevents Telegram from retrying the webhook due to a 500 response.
    """
    # Build the messages list by prepending history before the current message.
    #
    # The Anthropic API requires messages in strict alternating order:
    # user → assistant → user → assistant → ... → user (the current turn).
    #
    # history is a list of ConversationMessage Pydantic objects. We call
    # .model_dump() on each to get the {"role": ..., "content": ...} dict
    # that the Anthropic SDK expects. If history is None, (history or [])
    # produces an empty list and we fall back to a single-turn request.
    messages = [msg.model_dump() for msg in (history or [])]
    messages.append({"role": "user", "content": user_message})

    # Use the caller-supplied prompt when provided (e.g. coach.py injects
    # recent Strava data), otherwise fall back to the static constant.
    active_prompt = system_prompt or ""

    try:
        # messages.create is the core Anthropic API call.
        #
        # system= accepts either a plain string OR a list of content blocks.
        # We use the list form so we can attach cache_control to the system
        # prompt block — that's the only way the SDK exposes prompt caching on
        # the system turn. A plain string would work too, but wouldn't cache.
        #
        # messages= now contains the full conversation: all prior turns from
        # Supabase followed by the user's current message.
        #
        # max_tokens is required by the API (no default). 1024 is generous for
        # a Telegram coaching reply; increase if Claude starts truncating answers.
        response = await _client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": active_prompt,
                    # cache_control marks this block as a caching boundary.
                    # Anthropic will cache all tokens up to and including this
                    # block for ~5 minutes. See the PROMPT CACHING NOTE above
                    # for the 1024-token activation threshold caveat.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        )

        # response.content is a list of content blocks. For a plain text
        # response there will be exactly one block with type="text". We pull
        # the text out of it directly.
        #
        # If we ever add tool_use to the prompt (e.g. for Strava data lookups),
        # response.content will contain additional blocks and this line will
        # need to be updated to handle them.
        return response.content[0].text

    except anthropic.APIError as e:
        # APIError is the base class for all Anthropic-side errors:
        #   - AuthenticationError (bad or missing API key)
        #   - RateLimitError (too many requests)
        #   - APIConnectionError (network failure)
        #   - APIStatusError (4xx/5xx HTTP response from the API)
        #
        # We catch them all here and return a user-friendly message so that:
        #   1. The Telegram user gets a readable response instead of silence.
        #   2. FastAPI returns 200 to Telegram's webhook system.
        #   3. Telegram does NOT retry — retries on a broken key or rate limit
        #      would just queue up more failing requests.
        #
        # TODO: Replace print() with Python's logging module when the project
        # matures. Proper log levels (ERROR vs WARNING) and structured output
        # matter for debugging in production. For now, print() is fine.
        print(f"Claude API error: {type(e).__name__}: {e}")
        return "Sorry, I couldn't reach my brain right now. Try again in a moment."
