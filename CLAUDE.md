# Strava Coach Bot

## Project Overview
A personal AI cycling coach Telegram bot powered by Claude, integrated with Strava for training data and Supabase for persistence. Built as a learning project to develop Python backend, API integration, and deployment skills.

## Tech Stack
- **Backend**: Python 3.12+ with FastAPI
- **AI**: Anthropic Claude API (claude-sonnet-4-20250514)
- **Messaging**: Telegram Bot API via python-telegram-bot
- **Data**: Strava API v3 (OAuth2)
- **Database**: Supabase (PostgreSQL + Python client)
- **Future**: Docker containerization, GCP Cloud Run deployment

## Project Structure
```
strava-coach-bot/
├── CLAUDE.md
├── README.md
├── requirements.txt
├── .env                  # API keys (never commit)
├── .gitignore
├── app/
│   ├── __init__.py
│   ├── main.py           # FastAPI app entrypoint
│   ├── config.py         # Environment/settings via pydantic-settings
│   ├── routers/
│   │   ├── telegram.py   # Telegram webhook handler
│   │   └── strava.py     # Strava OAuth callback
│   ├── services/
│   │   ├── claude.py     # Claude API interaction
│   │   ├── strava.py     # Strava data fetching + token refresh
│   │   ├── supabase.py   # Database operations
│   │   └── coach.py      # Orchestrator: builds context, calls Claude
│   └── models/
│       └── schemas.py    # Pydantic models for API data
└── tests/
    └── ...
```

## Commands
- `uvicorn app.main:app --reload` — Start dev server
- `pip install -r requirements.txt` — Install dependencies
- `pytest` — Run tests
- `docker build -t strava-coach-bot .` — Build container (later)

## Conventions
- Type hints on all function signatures
- Async functions for all I/O (database, API calls, webhooks)
- Environment variables for all secrets — load via pydantic-settings, never hardcode
- Docstrings on public functions explaining what and why
- Keep services modular: each file handles one external integration
- Pydantic models for all data flowing between services

## Environment Variables Required
- `ANTHROPIC_API_KEY` — Claude API key
- `TELEGRAM_BOT_TOKEN` — From BotFather
- `STRAVA_CLIENT_ID` — Strava API app client ID
- `STRAVA_CLIENT_SECRET` — Strava API app client secret
- `SUPABASE_URL` — Supabase project URL
- `SUPABASE_KEY` — Supabase anon/service key

## Learning Goals
This is a learning project. When implementing new features:
1. Explain what the code does and why before writing it
2. Introduce one concept at a time
3. Prefer explicit over clever — readability matters more than conciseness
4. Add inline comments for non-obvious patterns (OAuth flows, webhook signatures, etc.)
5. When there's a choice between a library that hides complexity and writing it by hand, prefer the approach that teaches more — but flag the tradeoff

## Domain Context
- The user is a competitive road/gravel cyclist (~285W FTP, ~164 lbs, 7-15 hrs/week)
- Training is coach-directed with structured threshold and VO2max blocks
- Key Strava metrics: power (watts), TSS, duration, elevation, heart rate. But aggregate ride-level metrics aren't enough to get insights about a ride. The raw power, heart rate, and elevation data needs to be investigated. 
- The bot should be able to answer questions like: "How was my training load this week?", "Compare my last two Lookout Mountain efforts", "Am I ready for a big weekend ride?"
