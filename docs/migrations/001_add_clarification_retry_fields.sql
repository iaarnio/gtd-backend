-- Migration: Add clarification retry tracking fields to captures table
-- Created: 2026-02-08
-- Purpose: Enable automatic retry of failed clarifications with exponential backoff
--
-- New columns:
-- - clarify_status: Tracks LLM clarification state (pending/in_progress/completed/failed/permanently_failed)
-- - clarify_attempt_count: Number of clarification attempts made
-- - last_clarify_attempt_at: Timestamp of last clarification attempt

BEGIN TRANSACTION;

-- Add clarify_status column with default "pending" for existing rows
ALTER TABLE captures ADD COLUMN clarify_status VARCHAR(20) DEFAULT 'pending' NOT NULL;
CREATE INDEX idx_captures_clarify_status ON captures(clarify_status);

-- Add clarify_attempt_count column with default 0
ALTER TABLE captures ADD COLUMN clarify_attempt_count INTEGER DEFAULT 0 NOT NULL;

-- Add last_clarify_attempt_at column (nullable, NULL means not attempted yet)
ALTER TABLE captures ADD COLUMN last_clarify_attempt_at DATETIME NULL;

COMMIT;

-- Verification queries (run after migration):
-- SELECT COUNT(*) FROM captures WHERE clarify_status IS NULL; -- Should return 0
-- SELECT DISTINCT clarify_status FROM captures; -- Should show: 'pending'
-- SELECT COUNT(*) FROM captures WHERE clarify_attempt_count != 0; -- Should return 0 (new field)
