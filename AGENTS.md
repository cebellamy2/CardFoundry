# Repository Guidelines

## Project Structure & Module Organization

CardFoundry is a small FastAPI application organized as flat Python modules. `main.py` defines the web application, routes, and server-rendered HTML. `models.py` contains SQLAlchemy models, while `database.py` configures the local SQLite database and lightweight schema upgrades. Domain logic is separated into focused import, inventory, pricing, order, and maintenance service modules. Runtime data is stored in `cardfoundry.db`; treat it as local state, not source code. Automated tests live under `tests/`, and sanitized immutable production summaries live under `audits/`.

## Build, Test, and Development Commands

Create and activate a virtual environment before installing dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the development server with `uvicorn main:app --reload`; the application is then available at `http://127.0.0.1:8000`. Use `python -m compileall .` for a quick syntax check. No build step is required.

## Coding Style & Naming Conventions

Use Python 3 type hints and four-space indentation. Follow the existing style: `snake_case` for functions and variables, `PascalCase` for SQLAlchemy models, and uppercase names for module constants. Keep route handling in `main.py`, but place reusable business rules and external API logic in the relevant service module. Prefer small functions, explicit SQLAlchemy queries, and descriptive status values. No formatter or linter is configured, so keep changes consistent with nearby code and PEP 8.

## Testing Guidelines

Add focused `pytest` tests under `tests/`, using names such as `test_order_service.py` and `test_allocate_order_shortage`. Run them with `PYTHONPATH=. pytest -q`. Isolate tests from `cardfoundry.db` by using a temporary SQLite database, and mock Mana Pool or Scryfall HTTP requests. No automated test may call a live marketplace write endpoint. At minimum, run the full suite, Python compilation, an application import/home request, SQLite integrity, and `git diff --check` before submitting production changes.

## Commit & Pull Request Guidelines

Commits use short, imperative, conventional-commit-style summaries (`feat: ...`, `fix: ...`, `chore: ...`, `test: ...`). Keep commits narrowly scoped and describe the user-visible outcome. Pull requests should include a concise summary, verification steps, linked issues when applicable, and screenshots for HTML/UI changes. Call out schema changes and migration implications explicitly.

## Versioning & Releases

CardFoundry uses real [Semantic Versioning](https://semver.org/) starting at `1.0.0` (the verified production go-live baseline, commit `77562a9`). Every commit shipped to `main` is a release and gets its own version: `feat:` bumps MINOR, `fix:`/`test:`/`chore:` bump PATCH, a breaking change bumps MAJOR. As part of each release commit:

1. Update the `VERSION` file (single line, e.g. `1.38.0`) to the new version.
2. Add an entry to `CHANGELOG.md` (Keep a Changelog format) under that version.
3. After pushing, tag the commit `vMAJOR.MINOR.PATCH` and push the tag.

The current version is read from `VERSION` at import time (`main.APP_VERSION`) and shown in the footer of every page -- confirm it updated after a deploy rather than assuming.

## Security & Configuration

Configure `MANAPOOL_EMAIL` and `MANAPOOL_API_TOKEN` through a local `.env` file. Never commit credentials, customer exports, or `cardfoundry.db`. Avoid tests that call production marketplace APIs or mutate live inventory.
