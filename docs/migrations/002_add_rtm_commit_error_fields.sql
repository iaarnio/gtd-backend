-- Migration: Add RTM commit error tracking fields to captures table
-- Created: 2026-02-08
-- Purpose: Enable better error classification and operator visibility for RTM commit failures
--
-- New columns:
-- - commit_attempt_count: Number of RTM commit attempts made
-- - commit_error_message: Detailed error message from last commit failure

BEGIN TRANSACTION;

-- Add commit_attempt_count column with default 0
ALTER TABLE captures ADD COLUMN commit_attempt_count INTEGER DEFAULT 0 NOT NULL;

-- Add commit_error_message column (nullable, contains error details)
ALTER TABLE captures ADD COLUMN commit_error_message TEXT NULL;

COMMIT;

-- Verification queries (run after migration):
-- SELECT COUNT(*) FROM captures WHERE commit_attempt_count IS NULL; -- Should return 0
-- SELECT COUNT(*) FROM captures WHERE commit_error_message IS NOT NULL; -- May have values from phase 1 commits
-- SELECT DISTINCT commit_status FROM captures; -- Should show: 'pending', 'committed', 'failed', etc.
