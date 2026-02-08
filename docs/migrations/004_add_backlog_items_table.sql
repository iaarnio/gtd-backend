-- Migration 004: Add BacklogItem table for batch import backlog
--
-- Stores tasks imported in bulk from RTM or other sources.
-- These tasks are slowly clarified (e.g., 5 per day) and fed into
-- the same approve→commit pipeline as email captures.

CREATE TABLE IF NOT EXISTS backlog_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Raw task text (one task per line, as provided by user)
    raw_text TEXT NOT NULL,

    -- Source identifier
    source TEXT NOT NULL DEFAULT 'rtm-export',

    -- Status: pending, processing, processed, failed
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'processing', 'processed', 'failed')),

    -- Timestamps
    imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at DATETIME,

    -- Clarification tracking
    clarify_attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,

    -- Record metadata
    created_at_db DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Indices for efficient querying
CREATE INDEX IF NOT EXISTS idx_backlog_status ON backlog_items(status);
CREATE INDEX IF NOT EXISTS idx_backlog_imported_at ON backlog_items(imported_at);

-- Verify creation
SELECT 'BacklogItem table created successfully' as status;
