#!/usr/bin/env bash
# deploy.sh — Build the Docker image and deploy to Cloud Run.
#
# BEFORE FIRST USE:
#   1. Run scripts/setup_secrets.sh to create the Secret Manager containers.
#   2. Add a version to each secret (see instructions in setup_secrets.sh).
#   3. Fill in SUPABASE_URL and STRAVA_CLIENT_ID below.
#   4. Grant the Cloud Run service account access to each secret:
#        gcloud secrets add-iam-policy-binding SECRET_NAME \
#          --member="serviceAccount:573011463759-compute@developer.gserviceaccount.com" \
#          --role="roles/secretmanager.secretAccessor" \
#          --project=strava-coach-bot
#
# SECRETS: The four sensitive credentials are pulled from Secret Manager at
# deploy time via --set-secrets. They are never written to this file, the
# command line, or any log.
#
# ENV VARS: Non-sensitive configuration is passed via --set-env-vars. These
# values are not credentials and are safe to commit in this script.
#
# NOTE: Both --set-secrets and --set-env-vars REPLACE all existing values on
# the service — this script is the source of truth. Any vars set manually in
# the GCP Console will be cleared on the next deploy.

set -euo pipefail

readonly PROJECT="strava-coach-bot"
readonly REGION="us-central1"
readonly SERVICE="strava-coach-bot"
readonly IMAGE="us-central1-docker.pkg.dev/${PROJECT}/${SERVICE}/${SERVICE}:latest"

# ---------------------------------------------------------------------------
# Non-sensitive env vars — safe to commit. Update here if they change.
# ---------------------------------------------------------------------------

# SUPABASE_URL is the project endpoint URL, not a credential.
# Find it in: Supabase dashboard → Project Settings → API → Project URL
SUPABASE_URL="https://plaiuftdaimbtkqhdhjd.supabase.co"

# STRAVA_CLIENT_ID is the public OAuth app identifier, shown in browser flows.
# Find it in: strava.com/settings/api → Client ID
STRAVA_CLIENT_ID=156287

# Prod redirect URI — must match the Authorization Callback Domain in your
# Strava API app settings. Update if the Cloud Run service URL changes.
STRAVA_REDIRECT_URI="https://strava-coach-bot-573011463759.us-central1.run.app/strava/callback"

# ---------------------------------------------------------------------------
# Step 1: Build and push the Docker image to Artifact Registry.
# ---------------------------------------------------------------------------
echo "Building and pushing ${IMAGE}..."
gcloud builds submit \
    --tag "${IMAGE}" \
    --project "${PROJECT}"

# ---------------------------------------------------------------------------
# Step 2: Assemble the secret and env var specs.
# ---------------------------------------------------------------------------

# --set-secrets format: ENV_VAR_NAME=SECRET_RESOURCE_NAME:VERSION
# :latest resolves to the most recently added version at deploy time.
#
# Production-grade practice: pin to a specific version number (e.g. :3) so
# deploys are deterministic — a rotation won't silently change behavior until
# you deliberately bump the version here. Update after each rotation.
SECRETS_SPEC=(
    "ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest"
    "TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest"
    "STRAVA_CLIENT_SECRET=STRAVA_CLIENT_SECRET:latest"
    "SUPABASE_KEY=SUPABASE_KEY:latest"
)

ENV_VARS_SPEC=(
    "SUPABASE_URL=${SUPABASE_URL}"
    "STRAVA_CLIENT_ID=${STRAVA_CLIENT_ID}"
    "STRAVA_REDIRECT_URI=${STRAVA_REDIRECT_URI}"
)

# Join each array with commas for the gcloud flag value.
SECRETS_FLAG="$(IFS=','; echo "${SECRETS_SPEC[*]}")"
ENV_VARS_FLAG="$(IFS=','; echo "${ENV_VARS_SPEC[*]}")"

# ---------------------------------------------------------------------------
# Step 3: Deploy to Cloud Run.
# ---------------------------------------------------------------------------
echo "Deploying ${SERVICE} to ${REGION}..."
gcloud run deploy "${SERVICE}" \
    --image "${IMAGE}" \
    --region "${REGION}" \
    --platform managed \
    --project "${PROJECT}" \
    --set-secrets="${SECRETS_FLAG}" \
    --set-env-vars="${ENV_VARS_FLAG}"

echo ""
echo "Deploy complete."
echo "Service URL: https://strava-coach-bot-573011463759.us-central1.run.app"
