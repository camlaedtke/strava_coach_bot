#!/usr/bin/env python3
"""Quick smoke-tests for guard_prod_db.py. Run directly: python3 .claude/hooks/test_guard.py"""
import json, subprocess, sys

HOOK = [sys.executable, ".claude/hooks/guard_prod_db.py"]

CASES = [
    # (description, command_string, expect_blocked)
    ("prod ref + backfill script → BLOCK",
     "python scripts/backfill_activities.py --env-file dot-env-prod-placeholder",
     True),
    ("prod ref + DROP TABLE → BLOCK",
     "psql postgresql://postgres.PROD_REF:pw@host/db -c 'DROP TABLE users'",
     True),
    ("prod ref only, no mutation → ALLOW",
     "cat dot-env-prod-placeholder",
     False),
    ("dev mutation, no prod ref → ALLOW",
     "psql $DEV_URL -c 'DROP TABLE test'",
     False),
    ("INSERT against prod → BLOCK",
     "psql PROD_REF_URL -c 'INSERT INTO users VALUES (1)'",
     True),
    ("SELECT against prod → ALLOW",
     "psql PROD_REF_URL -c 'SELECT * FROM users'",
     False),
]

# Replace placeholders with real patterns at runtime (so this file itself doesn't trigger the hook)
PROD_REF = "plaiuftdaimbtkqhdhjd"
ENV_PROD = ".env.prod"

passed = failed = 0
for desc, cmd_template, expect_blocked in CASES:
    cmd = cmd_template.replace("PROD_REF", PROD_REF).replace("dot-env-prod-placeholder", ENV_PROD)
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    result = subprocess.run(HOOK, input=payload, text=True, capture_output=True)
    blocked = result.returncode == 2
    ok = blocked == expect_blocked
    status = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"[{status}] {desc}")
    if not ok:
        print(f"       expected blocked={expect_blocked}, got exit={result.returncode}")
        if result.stderr:
            print(f"       stderr: {result.stderr.strip()}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
