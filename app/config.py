"""
app/config.py — Application settings loaded from environment variables.

pydantic-settings gives us a Settings class where each attribute is an
environment variable. When Settings() is instantiated, it reads values
from the process environment AND from a .env file (if present). If a
required variable is missing at startup, the app crashes immediately with
a clear error — better than a mysterious AttributeError at runtime.

Usage in other modules:
    from app.config import settings
    token = settings.TELEGRAM_BOT_TOKEN
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central config object for the application.

    BaseSettings works like a regular Pydantic model, but instead of
    parsing a dict you pass in, it reads values from the environment.
    Variable names are case-insensitive by default (so TELEGRAM_BOT_TOKEN
    in .env maps to the TELEGRAM_BOT_TOKEN field here).
    """

    # SettingsConfigDict tells pydantic-settings where to look for values.
    # env_file=".env" means: also check a .env file in the working directory.
    # If the same variable is set in both the environment and .env, the real
    # environment variable wins — useful for production where secrets are
    # injected by the platform, not stored in files.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    # --- Required secrets (no default = startup error if missing) ---

    # The token BotFather gave you when you created the bot.
    # Telegram uses this to authenticate every API call we make.
    TELEGRAM_BOT_TOKEN: str

    # Anthropic — required now that Claude integration is live
    ANTHROPIC_API_KEY: str

    # Supabase — required now that conversation persistence is live
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # Strava — empty defaults so the app starts without them during dev
    STRAVA_CLIENT_ID: str = ""
    STRAVA_CLIENT_SECRET: str = ""

    # The URL Strava redirects to after the user grants access.
    # Must exactly match an "Authorization Callback Domain" registered in your
    # Strava API application settings at https://www.strava.com/settings/api
    # Dev: https://abc123.ngrok.io/strava/callback
    # Prod: https://your-domain.com/strava/callback
    STRAVA_REDIRECT_URI: str = ""


# Module-level singleton. Import this object everywhere rather than
# constructing Settings() multiple times — it avoids re-reading the
# environment on every import and keeps a single source of truth.
settings = Settings()
