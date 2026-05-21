#!/usr/bin/env python3
"""
PreToolUse hook: block Bash commands that would mutate the prod Supabase project.

Fires before every Bash tool call (wired via .claude/settings.json matcher: "Bash").
Reads the tool payload from stdin as JSON, checks two conditions:

  1. Does the command reference a prod identifier?
     - plaiuftdaimbtkqhdhjd  (prod Supabase project ref, embedded in the URL)
     - .env.prod             (env file that points to prod Supabase)

  2. Does the command contain a mutation signal?
     - SQL DDL: CREATE/DROP/ALTER TABLE, TRUNCATE, CREATE/DROP INDEX, etc.
     - SQL DML: INSERT INTO, UPDATE <table>, DELETE FROM
     - Known mutation scripts: backfill_activities.py, backfill_power_prs.py
     - Tool commands: supabase migration, supabase db push/reset

If BOTH conditions match → exit 2 (blocks the tool call; stderr shown as reason).
If either condition is absent → exit 0 (allow).

Exit 0 on any parse error — hook failures must never block legitimate work.
"""

import json
import re
import sys

# Prod Supabase identifiers. Both appear in connection strings, URLs, and .env flags.
PROD_PATTERNS = [
    r"plaiuftdaimbtkqhdhjd",  # project ref embedded in SUPABASE_URL
    r"\.env\.prod",           # --env-file .env.prod flag used by backfill scripts
]

# Commands or SQL keywords that write to the database.
MUTATION_PATTERNS = [
    # SQL DDL
    r"\bCREATE\s+TABLE\b",
    r"\bDROP\s+TABLE\b",
    r"\bALTER\s+TABLE\b",
    r"\bTRUNCATE\b",
    r"\bCREATE\s+INDEX\b",
    r"\bDROP\s+INDEX\b",
    r"\bCREATE\s+SEQUENCE\b",
    r"\bDROP\s+SEQUENCE\b",
    r"\bDROP\s+SCHEMA\b",
    # SQL DML
    r"\bINSERT\s+INTO\b",
    r"\bUPDATE\s+\w",
    r"\bDELETE\s+FROM\b",
    # Supabase / migration CLI commands
    r"supabase\s+migration",
    r"supabase\s+db\s+push",
    r"supabase\s+db\s+reset",
    # Project backfill scripts (both write to Supabase)
    r"backfill_activities\.py",
    r"backfill_power_prs\.py",
]


def _first_match(patterns: list[str], text: str) -> str | None:
    """Return the first pattern that matches text (case-insensitive), or None."""
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # Never block on a parse failure — allow and move on.
        sys.exit(0)

    command: str = payload.get("tool_input", {}).get("command", "")

    prod_match = _first_match(PROD_PATTERNS, command)
    if prod_match is None:
        sys.exit(0)  # no prod identifier → safe to allow

    mutation_match = _first_match(MUTATION_PATTERNS, command)
    if mutation_match is None:
        sys.exit(0)  # prod reference but read-only operation → allow

    # Both matched — block.
    print(
        f"BLOCKED — prod Supabase mutation detected.\n"
        f"  Identifier matched : {prod_match}\n"
        f"  Mutation matched   : {mutation_match}\n"
        f"\n"
        f"To apply migrations intentionally : /migrate prod --confirm\n"
        f"To run a backfill against prod    : temporarily comment out this hook in\n"
        f"  .claude/settings.json, run the script, then restore the hook.",
        file=sys.stderr,
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
