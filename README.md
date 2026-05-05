# Strava Coach Bot

A personal AI cycling coach Telegram bot powered by Claude, integrated with Strava for training data and Supabase for persistence.

## Prerequisites

- Python 3.11+
- [ngrok](https://ngrok.com/) (for local development — Telegram needs a public HTTPS URL to deliver webhooks)
- Accounts and API credentials for: Telegram, Anthropic, Strava, Supabase

## 1. Clone and install dependencies

```bash
git clone <repo-url>
cd strava-coach-bot
pip install -r requirements.txt
```

## 2. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/botfather)
2. Send `/newbot` and follow the prompts
3. Copy the bot token — you'll need it for `TELEGRAM_BOT_TOKEN`

## 3. Create a Supabase project

1. Go to [supabase.com](https://supabase.com) and create a new project
2. In the SQL editor, run the following to create the required tables:

```sql
CREATE TABLE users (
    id               BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL UNIQUE,
    first_name       TEXT NOT NULL,
    username         TEXT,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE messages (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES users(id),
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE strava_tokens (
    id                BIGSERIAL PRIMARY KEY,
    telegram_user_id  BIGINT NOT NULL UNIQUE,
    access_token      TEXT NOT NULL,
    refresh_token     TEXT NOT NULL,
    expires_at        BIGINT NOT NULL,
    strava_athlete_id BIGINT,
    updated_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE activity_metrics (
    id               BIGSERIAL PRIMARY KEY,
    activity_id      BIGINT NOT NULL UNIQUE,
    telegram_user_id BIGINT NOT NULL,
    streams          JSONB  NOT NULL,
    metrics          JSONB  NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT now()
);
```

3. From the project settings, copy the **Project URL** and **anon/service key**

## 4. Create a Strava API application

1. Go to [strava.com/settings/api](https://www.strava.com/settings/api) and create an app
2. Set **Authorization Callback Domain** to your ngrok domain (e.g. `abc123.ngrok.io`) — update this whenever your ngrok URL changes
3. Copy the **Client ID** and **Client Secret**

## 5. Configure environment variables

Create a `.env` file in the project root:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
ANTHROPIC_API_KEY=your_anthropic_key_here
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_anon_key_here
STRAVA_CLIENT_ID=your_strava_client_id
STRAVA_CLIENT_SECRET=your_strava_client_secret
STRAVA_REDIRECT_URI=https://your-ngrok-subdomain.ngrok.io/strava/callback
```

## 6. Start the server

```bash
uvicorn app.main:app --reload
```

The server runs on `http://localhost:8000`.

## 7. Expose the server with ngrok

In a separate terminal:

```bash
ngrok http 8000
```

Copy the `https://` forwarding URL (e.g. `https://abc123.ngrok.io`).

## 8. Register the Telegram webhook

Tell Telegram where to send updates:

```bash
curl -X POST "http://localhost:8000/telegram/set-webhook?url=https://abc123.ngrok.io/telegram/webhook"
```

## 9. Connect Strava

1. Message your bot on Telegram to get your Telegram user ID (it appears in the server logs on the first message)
2. Visit this URL in a browser, replacing the ID:
   ```
   http://localhost:8000/strava/auth?telegram_user_id=YOUR_TELEGRAM_USER_ID
   ```
3. Open the returned `auth_url` in a browser and authorize the app on Strava
4. After redirect, you should see `"Strava connected successfully!"`

## 10. (Optional) Backfill historical activities

To pre-populate the metrics cache with past Strava activities so the bot has context without waiting for new rides:

```bash
python scripts/backfill_activities.py
```

## Verify everything works

- `GET http://localhost:8000/health` should return `{"status": "ok"}`
- Send a message to your bot on Telegram — it should respond with coaching analysis

## Notes

- The ngrok URL changes on each restart (unless you have a paid ngrok account). When it changes, update `STRAVA_REDIRECT_URI` in `.env`, update the callback domain in your Strava app settings, and re-run the webhook registration in step 8.
- `FTP` is hardcoded to `290` in `app/services/coach.py` — update this to match your current FTP.
