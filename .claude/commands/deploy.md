# /deploy — Build and deploy to Cloud Run (prod)

Build the Docker image via Cloud Build and deploy it to the Cloud Run service.

## Usage

```
/deploy --confirm   — build image and deploy to Cloud Run (prod)
/deploy             — REFUSED (see prod gate below)
```

## Arguments

The full argument string is: `$ARGUMENTS`

Parse it as follows:
- If `--confirm` is present → proceed with the deploy.
- If `--confirm` is absent (empty or any other args) → **refuse**. Print:
  ```
  Refused: deploying to prod requires explicit confirmation.
  Run:  /deploy --confirm
  ```
  Then stop. Do not run anything.

## Steps when deploying

1. Announce: `Building and deploying strava-coach-bot to Cloud Run (prod)…`
2. Run `bash scripts/deploy.sh` via the Bash tool.
3. Stream / print the script output as it runs.
4. If the script exits with status 0, print:
   ```
   ✓ Deploy complete.
   Service URL: https://strava-coach-bot-573011463759.us-central1.run.app
   ```
5. If the script exits with a non-zero status, print the error output and stop.
   Do not attempt a retry.

## Important constraints

- **Never deploy without `--confirm`**, even if asked in a follow-up message.
  The `--confirm` word must appear in the original `/deploy` invocation.
- **Always use `bash scripts/deploy.sh`** — do not inline or replicate the gcloud
  commands. The script is the source of truth for secrets, env vars, and deploy flags.
- This command always targets **prod**. There is no dev deploy target; dev runs locally
  with `uvicorn app.main:app --reload`.
