# Strava Coach Bot

## Project Overview

A personal AI cycling coach Telegram bot powered by Claude, integrated with Strava for training data and Supabase for persistence. Built as a learning project to develop Python backend, API integration, and deployment skills.

## Tech Stack

- **Backend**: Python 3.11+ with FastAPI
- **AI**: Anthropic Claude API (claude-sonnet-4-6)
- **Messaging**: Telegram Bot API via python-telegram-bot
- **Data**: Strava API v3 (OAuth2)
- **Database**: Supabase (PostgreSQL + async Python client)
- **Future**: Docker containerization, GCP Cloud Run deployment

## Project Structure

```
strava-coach-bot/
├── CLAUDE.md
├── requirements.txt
├── .env                  # API keys (never commit)
├── .gitignore
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app entrypoint + lifespan shutdown
│   ├── config.py         # Environment/settings via pydantic-settings
│   ├── routers/
│   │   ├── telegram.py   # Telegram webhook + /command dispatch
│   │   └── strava.py     # Strava OAuth callback + auth URL endpoint
│   ├── services/
│   │   ├── claude.py     # Claude API interaction (prompt caching, history)
│   │   ├── strava.py     # Strava data fetching, token refresh, stream fetch
│   │   ├── supabase.py   # Database operations (users, messages, tokens, metrics cache)
│   │   ├── metrics.py    # Pure metric computation from stream data (no I/O)
│   │   └── coach.py      # Orchestrator: fetch-or-cache streams, build prompt, call Claude
│   └── models/
│       └── schemas.py    # Pydantic models for Telegram, Strava, and DB data
└── scripts/
    └── backfill_activities.py  # One-time script to backfill historical activity metrics
```

No `tests/` directory exists yet.

## Commands

- `uvicorn app.main:app --reload` — Start dev server
- `pip install -r requirements.txt` — Install dependencies
- `python scripts/backfill_activities.py` — Backfill historical Strava activities into cache
- `docker build -t strava-coach-bot .` — Build container (later)

## API Endpoints

- `POST /telegram/webhook` — Receives Telegram updates; full coach pipeline
- `POST /telegram/set-webhook?url=<url>` — Dev utility: register webhook URL with Telegram
- `GET  /strava/auth?telegram_user_id=<id>` — Returns Strava OAuth authorization URL
- `GET  /strava/callback` — Strava redirects here after OAuth; saves tokens
- `GET  /health` — Health check, returns `{"status": "ok"}`

## Supabase Schema

All four tables must exist (run once in Supabase SQL editor):

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
    expires_at        BIGINT NOT NULL,   -- Unix timestamp
    strava_athlete_id BIGINT,
    updated_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE activity_metrics (
    id               BIGSERIAL PRIMARY KEY,
    activity_id      BIGINT NOT NULL UNIQUE,   -- Strava activity ID (globally unique)
    telegram_user_id BIGINT NOT NULL,
    streams          JSONB  NOT NULL,           -- raw stream arrays from Strava
    metrics          JSONB  NOT NULL,           -- computed ActivityMetrics dict
    created_at       TIMESTAMPTZ DEFAULT now()
);
```

Raw streams are stored alongside computed metrics so formulas can be recomputed without re-fetching from Strava.

## Key Constants

- `FTP = 290` in `coach.py` — athlete's FTP in watts; used for all zone calculations
- `STREAM_ACTIVITY_COUNT = 5` in `coach.py` — number of recent cycling activities to fetch full stream data for (each cache miss = 1 Strava API call)
- `HISTORY_LIMIT = 20` in `supabase.py` — conversation turns passed to Claude as context (~10 exchanges)
- `CLAUDE_MODEL = "claude-sonnet-4-6"` in `claude.py`

## Conventions

- Type hints on all function signatures
- Async functions for all I/O (database, API calls, webhooks)
- Environment variables for all secrets — load via pydantic-settings, never hardcode
- Docstrings on public functions explaining what and why
- Keep services modular: each file handles one external integration
- Pydantic models for all data flowing between services
- `metrics.py` is pure Python (no I/O, no async) — all metric functions take lists, return values
- Fetch-or-cache pattern for Strava streams: check `activity_metrics` table first, only call Strava API for unseen activities
- `asyncio.gather()` for concurrent stream fetches when multiple cache misses occur
- Lazy singleton pattern for service clients: `supabase.py` uses async `acreate_client()` (must await inside event loop), `strava.py` uses sync `httpx.AsyncClient()` (safe at module level)
- Command responses (`/strava`) are NOT saved to the messages table — we don't want bot-command text in Claude's conversation context

## Environment Variables Required

- `ANTHROPIC_API_KEY` — Claude API key
- `TELEGRAM_BOT_TOKEN` — From BotFather
- `STRAVA_CLIENT_ID` — Strava API app client ID
- `STRAVA_CLIENT_SECRET` — Strava API app client secret
- `STRAVA_REDIRECT_URI` — OAuth callback URL (must match Strava app settings)
- `SUPABASE_URL` — Supabase project URL
- `SUPABASE_KEY` — Supabase anon/service key

`STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, and `STRAVA_REDIRECT_URI` default to `""` so the app starts without them during development.

## Learning Goals

This is a learning project. When implementing new features:

1. Explain what the code does and why before writing it
2. Introduce one concept at a time
3. Prefer explicit over clever — readability matters more than conciseness
4. Add inline comments for non-obvious patterns (OAuth flows, webhook signatures, etc.)
5. When there's a choice between a library that hides complexity and writing it by hand, prefer the approach that teaches more — but flag the tradeoff

## Domain Context

### Athlete Profile

- Competitive road/gravel cyclist, ~280-310W FTP (constantly improving, so exact value is in flux). Assume 290W for now (`FTP = 290` constant in `coach.py`). Weight ~164 lbs (74 kg), 7–15 hrs/week
- Training is coach-directed with structured threshold and VO2max blocks
- Goals: performance in road and gravel events

**Always prefer streams over aggregate.** Aggregate fields can be misleading — a flat
average power says nothing about whether the athlete rode steadily at threshold or
surged repeatedly in Z5/Z6 and coasted in Z1.

### What Streams Enable (all implemented in `metrics.py`)

- **Normalized Power (NP)** — 30-second rolling average of watts → raised to 4th power →
  mean of that → 4th root. Better represents the physiological cost of variable-pace riding.
- **Variability Index (VI)** — NP / average_power. A VI near 1.0 means steady effort;
  VI > 1.05 on a flat ride suggests poor pacing.
- **Time in zones** — seconds spent in each of Z1–Z6 (Coggan 6-zone model), calculated
  from the raw watts stream. Much more informative than average power alone.
- **Power duration curve** — best average power for 5s, 1m, 5m, 20m, 60m using O(n)
  sliding window sums.
- **HR decoupling** — compares the power:HR efficiency ratio in the first half of the ride
  vs. the second half. > ~5% indicates aerobic drift.
- **Climb segments** — sections where `grade_smooth` stays above 4% for >= 60 seconds,
  extracted with their own power/HR/duration sub-summaries. Capped at 3 per activity in the prompt.

Raw streams are never passed directly to Claude (too many tokens). All metrics are
pre-computed in `metrics.py` and injected as formatted text into the system prompt.

### Activity Formatting

`coach.py` converts all Strava units to imperial for display (athlete's native system):
- distance: meters → miles
- elevation: meters → feet
- moving_time: seconds → H:MM

Activities with cached metrics get the full rich format (NP, VI, zones, PDC, climbs).
The remaining activities in the last-10 summary get aggregate-only format.

### Caching and API Calls

`coach.py` fetches the 10 most recent activity summaries, then loads full stream data
for the `STREAM_ACTIVITY_COUNT` (currently 5) most recent cycling activities.
Computed metrics and raw streams are stored in the `activity_metrics` Supabase table
on first fetch. Subsequent messages use cached metrics — no Strava API call needed for
seen activities. The backfill script (`scripts/backfill_activities.py`) pre-populates
the cache for historical activities.

### Bot Commands

- `/strava` — shows Strava connection status or sends the OAuth authorization URL

### Example Questions the Bot Should Handle

- "How was my training load this week?" — zone distribution, hours, NP trends
- "Compare my last two Lookout Mountain efforts" — NP, VI, time, climb segment power
- "Am I ready for a big weekend ride?" — recent load, HR decoupling trend
- "Was my threshold workout actually threshold?" — time-in-Z4 vs Z3/Z5 split
