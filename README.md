# GTD Backend

Automatically captures task ideas from Gmail, clarifies them with AI, and syncs approved tasks to Remember The Milk.

## Quick Start

1. **Set up credentials** in `.env` (Gmail, RTM, LLM API)
2. **Run** `docker-compose up -d`
3. **Send emails** to `gtdinput` label
4. **Approve tasks** at http://localhost:8000/approvals
5. Tasks sync to RTM automatically

## How It Works

Email → AI clarification → You approve → RTM sync

## Features

- **AI Clarification** - Automatically classifies captures as projects or next actions
- **Search & Filter** - Audit log with text search and status filters
- **Anchor Task** - Daily RTM reminder when approvals are pending
- **Health & Metrics** - System status monitoring
- **Structured Logging & Alerts** - JSON logs with error tracking (customize as needed)

## RTM Convention

Uses specific naming for my GTD workflow:
- Projects: Finnish name + uppercase shortname (e.g., KEITTIÖ)
- Next actions: Tagged with `#na`
- Archive: "Archived" list for completed tasks

**This is a personalized setup. Adapt the RTM naming convention and alert rules to match your system.**
