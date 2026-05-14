# Database Migrations

Migrations are plain `.sql` files numbered sequentially. Apply them in order to both
the dev and prod Supabase projects whenever the schema changes.

## Naming convention

```
NNN_short_description.sql
```

- `NNN` — zero-padded sequence number (001, 002, …)
- description — snake_case summary of what changed

## How to apply a migration

Open the Supabase SQL editor for the target project and paste + run the file contents.
Do this for **dev first**, confirm it works, then repeat for **prod**.

There is no automated runner — every migration is applied manually and intentionally.

## Applied migrations

| # | File | What it does |
|---|------|--------------|
| 001 | `001_initial_schema.sql` | Creates all five baseline tables: users, messages, strava_tokens, activity_metrics, power_prs |

## Adding a new migration

1. Create `migrations/NNN_description.sql` (increment NNN).
2. Write the change as a non-destructive `ALTER TABLE` / `CREATE INDEX` / etc. — never
   drop a column without confirming it's unused in both environments.
3. Apply to dev, test, then apply to prod.
4. Add a row to the "Applied migrations" table above.
5. Update the schema section in `CLAUDE.md` to reflect the new state.
