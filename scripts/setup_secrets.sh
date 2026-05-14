#!/usr/bin/env bash
# setup_secrets.sh — Create Secret Manager secret containers for sensitive credentials.
#
# IDEMPOTENT: safe to run multiple times. Creates the secret "container" (the
# GCP resource) but does NOT add any versions (the actual secret values).
# Values must be added manually — keeping them out of scripts, command history,
# and logs is the whole point.
#
# Run this once when setting up a new GCP project, then add versions manually.
# See scripts/deploy.sh for how secrets are mounted at deploy time.
#
# =============================================================================
# HOW TO ADD A SECRET VERSION (do this for each secret after running this script)
# =============================================================================
#
# Use printf, NOT echo, when piping a value. echo appends a trailing newline
# (\n) that becomes part of the stored secret value. Most clients strip it
# silently, but some don't — a stray newline can cause auth failures that are
# painful to debug. printf '%s' avoids the newline entirely.
#
#   printf '%s' 'your-actual-secret-value' | \
#     gcloud secrets versions add SECRET_NAME \
#       --data-file=- \
#       --project=strava-coach-bot
#
# Or read from a file (safest — value never touches shell history or the screen):
#   gcloud secrets versions add SECRET_NAME \
#     --data-file=/path/to/secret.txt \
#     --project=strava-coach-bot
#
# To confirm a version was added:
#   gcloud secrets versions list SECRET_NAME --project=strava-coach-bot
#
# To read back the stored value (to verify it's correct, before deploying):
#   gcloud secrets versions access latest \
#     --secret=SECRET_NAME \
#     --project=strava-coach-bot
# =============================================================================

set -euo pipefail

readonly PROJECT="strava-coach-bot"

# The four sensitive credentials that Secret Manager owns in deployed
# environments. These map to ENV_VAR_NAME=SECRET_NAME:latest in deploy.sh.
readonly SECRETS=(
    "ANTHROPIC_API_KEY"
    "TELEGRAM_BOT_TOKEN"
    "STRAVA_CLIENT_SECRET"
    "SUPABASE_KEY"
)

create_secret_if_missing() {
    local name="$1"
    if gcloud secrets describe "${name}" --project="${PROJECT}" &>/dev/null; then
        echo "  [exists]  ${name}"
    else
        gcloud secrets create "${name}" \
            --project="${PROJECT}" \
            --replication-policy="automatic"
        echo "  [created] ${name}"
    fi
}

echo "Setting up Secret Manager secrets in project: ${PROJECT}"
echo ""

for secret in "${SECRETS[@]}"; do
    create_secret_if_missing "${secret}"
done

echo ""
echo "Done. Secret containers are ready."
echo "Next step: add a version to each secret using the instructions at the"
echo "top of this file. No values were set — that's intentional."
