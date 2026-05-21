# /migrate — Apply database migrations

Apply SQL migration files from `migrations/` to a target Supabase project, in filename order.

## Usage

```
/migrate dev              — apply to dev Supabase (no confirmation required)
/migrate prod --confirm   — apply to prod Supabase (--confirm is mandatory)
/migrate prod             — REFUSED (see prod gate below)
```

## Arguments

The full argument string is: `$ARGUMENTS`

Parse it as follows:
- If empty or unrecognized → print the usage block above and stop.
- If `dev` → target is dev (project ref `ztxymmppambxjhgqlftr`, use `supabase-dev-write` MCP).
- If `prod --confirm` (both words present) → target is prod (project ref `plaiuftdaimbtkqhdhjd`,
  use `supabase-prod-write` MCP).
- If `prod` without `--confirm` → **refuse**. Print:
  ```
  Refused: applying migrations to prod requires explicit confirmation.
  Run:  /migrate prod --confirm
  ```
  Then stop. Do not apply anything.

## Steps when applying

1. List all files in `migrations/` and filter to `*.sql` files only.
2. Sort them by filename (lexicographic order — `001_` sorts before `002_`, etc.).
3. For each file in order:
   a. Read the file contents.
   b. Announce: `Applying <filename> to <target>…`
   c. Execute the SQL using `execute_sql` on the appropriate MCP server
      (`supabase-dev-write` for dev, `supabase-prod-write` for prod).
   d. If the call succeeds, print: `✓ <filename> applied`
   e. If the call fails, print the error and **stop immediately** — do not apply further files.
4. After all files succeed, print a summary: how many files were applied and to which target.

## Important constraints

- **Never apply to prod without `--confirm`**, even if asked in a follow-up message.
  The `--confirm` word must appear in the original `/migrate` invocation.
- Apply files **strictly in filename order**. Never re-order or skip.
- **Stop on first failure.** Schema migrations are not idempotent by default — applying a
  later migration after an earlier one failed would leave the schema in an inconsistent state.
- Do not run any Bash commands to apply SQL. Use only the MCP `execute_sql` tool.
  (The PreToolUse Bash hook would block prod mutations via Bash anyway.)
- If `migrations/` contains non-`.sql` files (e.g., `README.md`), skip them silently.
