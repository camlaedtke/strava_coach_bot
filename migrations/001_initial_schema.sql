-- Migration 001: initial schema
-- All five tables that make up the baseline schema.
-- Apply to both dev and prod Supabase projects before running the app for the first time.

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

-- Raw stream arrays are stored alongside computed metrics so formulas can be
-- recomputed without re-fetching from Strava.
CREATE TABLE activity_metrics (
    id               BIGSERIAL PRIMARY KEY,
    activity_id      BIGINT NOT NULL UNIQUE,   -- Strava activity ID (globally unique)
    telegram_user_id BIGINT NOT NULL,
    streams          JSONB  NOT NULL,           -- raw stream arrays from Strava
    metrics          JSONB  NOT NULL,           -- computed ActivityMetrics dict
    created_at       TIMESTAMPTZ DEFAULT now()
);

-- records is a JSONB dict mapping duration label → best watts
-- e.g. {"15s": 720.0, "1m": 580.0, ...}
-- JSONB lets us add new duration keys without a schema migration.
CREATE TABLE power_prs (
    id               BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL UNIQUE,
    records          JSONB  NOT NULL DEFAULT '{}',
    updated_at       TIMESTAMPTZ DEFAULT now()
);
