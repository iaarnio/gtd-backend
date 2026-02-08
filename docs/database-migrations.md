# Database Migrations Guide

This document explains how to apply database migrations to the GTD backend SQLite database.

## Overview

The GTD backend uses SQLite for simplicity. Migrations are provided as SQL files in the `docs/migrations/` directory and should be applied manually to the production database.

## Current Migrations

### Migration 001: Add Clarification Retry Fields
**File:** `docs/migrations/001_add_clarification_retry_fields.sql`
**Purpose:** Enable automatic retry of failed LLM clarifications with exponential backoff
**Status:** Optional (required for Phase 4: Clarification Auto-Retry)

**New columns:**
- `clarify_status` (String, default='pending'): Tracks LLM clarification attempt status
  - `pending` - Not yet attempted
  - `in_progress` - Currently being clarified
  - `completed` - Successfully clarified
  - `failed` - Attempted but failed (will retry)
  - `permanently_failed` - Max retries exceeded (requires manual intervention)
- `clarify_attempt_count` (Integer, default=0): Number of clarification attempts made
- `last_clarify_attempt_at` (DateTime, nullable): Timestamp of last clarification attempt

**How to apply:**

Option 1: Using sqlite3 command line:
```bash
sqlite3 /app/data/gtd.db < docs/migrations/001_add_clarification_retry_fields.sql
```

Option 2: Using Python:
```python
import sqlite3
conn = sqlite3.connect('/app/data/gtd.db')
cursor = conn.cursor()
with open('docs/migrations/001_add_clarification_retry_fields.sql', 'r') as f:
    cursor.executescript(f.read())
conn.commit()
conn.close()
```

Option 3: Using FastAPI startup hook (automatic):
Add to `app/main.py` in the `initialize_database()` function:
```python
# Apply pending migrations
from .migrations import apply_migrations
apply_migrations(engine)
```

## Clarification Retry Backoff Schedule

After adding the clarification retry fields, the system uses exponential backoff for failed clarifications:

| Attempt | Delay from Previous Failure | Total Time |
|---------|---------------------------|-----------|
| 1 | immediate | 0s |
| 2 | 5 minutes | 5m |
| 3 | 30 minutes | 35m |
| 4 | 2 hours | 2h 35m |
| 5+ | permanently failed | N/A |

**Examples:**
- Capture #1 fails clarification at 10:00 AM
  - Attempt 1: 10:00 (failed)
  - Attempt 2: 10:05 (5 minutes later)
  - Attempt 3: 10:35 (30 minutes after attempt 2)
  - Attempt 4: 12:35 (2 hours after attempt 3)
  - Attempt 5: permanently_failed (next poll cycle)

- User can manually retry at any time by editing the clarification form

## Default Values

When migrations are applied to an existing database, all existing captures will have:
- `clarify_status = 'pending'` (meaning: needs first clarification attempt)
- `clarify_attempt_count = 0` (no attempts yet)
- `last_clarify_attempt_at = NULL` (never attempted)

**Note:** Captures that already have `clarify_json` populated will still have `clarify_status = 'pending'` because they haven't gone through the new retry system. On the next clarification poll, they will be skipped (since they have clarification already).

## Rolling Out Migrations

### Development
Migrations should be applied immediately to development database.

### Staging
Apply migrations to staging database and test the clarification retry logic:
1. Create a test capture
2. Force a clarification failure (disconnect network or use invalid API key)
3. Verify `clarify_status = 'failed'` and `clarify_attempt_count = 1`
4. Wait 5 minutes (or manipulate clock in test)
5. Run next clarification poll
6. Verify retry was attempted

### Production
1. Backup production database: `cp /app/data/gtd.db /app/data/gtd.db.backup`
2. Apply migration: `sqlite3 /app/data/gtd.db < docs/migrations/001_add_clarification_retry_fields.sql`
3. Verify with: `sqlite3 /app/data/gtd.db "PRAGMA table_info(captures);"`
4. Restart GTD backend service
5. Monitor logs for clarification retry behavior

## Verifying Migrations

After applying a migration, verify it with:

```sql
-- Check if columns exist and have correct defaults
PRAGMA table_info(captures);

-- Check current values (should all be defaults for new migration)
SELECT COUNT(*), clarify_status, COUNT(DISTINCT clarify_attempt_count)
FROM captures
GROUP BY clarify_status;

-- List captures that will be retried on next poll
SELECT id, clarify_status, clarify_attempt_count, last_clarify_attempt_at
FROM captures
WHERE decision_status = 'proposed'
  AND clarify_status IN ('pending', 'failed');
```

## Rollback Procedure

If migration needs to be rolled back (rare):

```sql
-- Remove columns (WARNING: data loss!)
ALTER TABLE captures DROP COLUMN clarify_status;
ALTER TABLE captures DROP COLUMN clarify_attempt_count;
ALTER TABLE captures DROP COLUMN last_clarify_attempt_at;

-- Or restore from backup:
-- cp /app/data/gtd.db.backup /app/data/gtd.db
```

Note: SQLite has limited ALTER TABLE support. If you need to remove columns, it's easier to restore from backup.

## Future Migrations

Future migrations should:
1. Follow the naming convention: `NNN_description.sql` (e.g., `002_add_foo_field.sql`)
2. Be placed in `docs/migrations/`
3. Include comments explaining purpose and impact
4. Include verification queries
5. Be idempotent (safe to run multiple times)
6. Be documented in this file with details and rollback procedure
