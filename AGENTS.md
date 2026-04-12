# Repository Guidelines

## Project Structure & Module Organization
- Core application code lives in `app/` (FastAPI routes, services, DB models, and integrations).
- HTML templates are in `app/templates/`.
- Tests mirror runtime modules in `tests/` (for example, `app/rtm_commit.py` → `tests/test_rtm_commit.py`).
- Operational docs and SQL migrations are under `docs/` and `docs/migrations/`.
- Runtime SQLite data is stored in `data/` (`gtd.db`), mounted by Docker.

## Build, Test, and Development Commands
- Create env and install deps: `python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt`.
- Run tests (preferred wrapper): `./scripts/test`.
- Run targeted tests: `./scripts/test -k approval`.
- Start app locally (no Docker): `.venv/bin/python -m uvicorn app.main:app --reload`.
- Start with containers: `docker-compose up -d` (serves on `http://localhost:8000`).

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indentation and explicit, descriptive names.
- Use `snake_case` for functions/variables/modules, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.
- Keep type hints on public functions and maintain concise docstrings where behavior is non-obvious.
- Keep modules focused (avoid mixing routing, DB, and external API logic in one function).

## Testing Guidelines
- Framework: `pytest` (invoked via `./scripts/test`, which uses `.venv`).
- Name tests as `test_*.py`; keep test functions descriptive (for example, `test_approval_prefills_existing_notes`).
- Add or update tests with every behavior change, especially around RTM sync, approval logic, and ingestion flows.
- Prefer factory/fixture reuse from `tests/conftest.py` and `tests/factories.py`.

## Commit & Pull Request Guidelines
- Use short, imperative commit subjects (`Fix retry delay parsing`, `Add backlog processing test`).
- Conventional prefixes (`feat:`, `fix:`) are acceptable but keep style consistent within a branch.
- PRs should include: purpose summary, key behavior changes, test evidence (`./scripts/test` output), and linked issue/task.
- Include screenshots only for UI/template changes (e.g., pages in `app/templates/`).

## Security & Configuration Tips
- Keep secrets in `.env`; never commit API keys or tokens.
- Validate required integrations (IMAP, RTM, LLM) via `/health` after config changes.
- When altering schema, add an idempotent SQL file in `docs/migrations/` and document rollout steps.
