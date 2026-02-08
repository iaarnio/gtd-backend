-- Migration 003: Add RtmTask table for daily highlights system
--
-- Tracks RTM tasks for the daily highlights feature:
-- - Caches RTM task metadata
-- - Tracks project association
-- - Stores completion status
-- - Manages suggestion history (anti-nag rule)

CREATE TABLE IF NOT EXISTS rtm_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- RTM identifiers (unique per RTM task)
    rtm_task_id TEXT UNIQUE NOT NULL,
    rtm_taskseries_id TEXT NOT NULL,
    rtm_list_id TEXT NOT NULL,

    -- Task metadata
    name TEXT NOT NULL,
    created_at DATETIME NOT NULL,

    -- Project association (NULL = lonely action, eligible for highlight)
    rtm_project_id TEXT,

    -- Completion status
    rtm_completed BOOLEAN NOT NULL DEFAULT FALSE,

    -- Cached tags as JSON array (e.g., ["#na", "work"])
    tags TEXT,

    -- Suggestion tracking for anti-nag rule
    times_suggested INTEGER NOT NULL DEFAULT 0,
    last_suggested_at DATETIME,

    -- Sync tracking
    last_synced_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Record creation time
    created_at_db DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Create indices for performance
CREATE INDEX IF NOT EXISTS idx_rtm_project_id ON rtm_tasks(rtm_project_id);
CREATE INDEX IF NOT EXISTS idx_rtm_completed ON rtm_tasks(rtm_completed);
CREATE INDEX IF NOT EXISTS idx_last_suggested_at ON rtm_tasks(last_suggested_at);

-- Verify creation
SELECT 'RtmTask table created successfully' as status;
