-- Migration 002: athlete_profile table
-- Stores per-user FTP and body weight so coaching context is dynamic rather
-- than a hardcoded constant. One row per user, keyed by telegram_user_id.
-- Rows are created on first explicit update; absent rows fall back to the
-- AthleteProfile Pydantic defaults in code (ftp_watts=293, weight_kg=74.0).

CREATE TABLE athlete_profile (
    id               BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT    NOT NULL UNIQUE,
    ftp_watts        INT,                           -- watts; NULL falls back to code default
    weight_kg        NUMERIC(5, 2),                 -- kg, fractional; NULL falls back to code default
    updated_at       TIMESTAMPTZ DEFAULT now()
);
