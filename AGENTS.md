# Repository Guidelines

## Project Structure & Module Organization

CardFoundry is a small FastAPI application organized as flat Python modules. `main.py` defines the web application, routes, and server-rendered HTML. `models.py` contains SQLAlchemy models, while `database.py` configures the local SQLite database and lightweight schema upgrades. Domain logic is separated into `import_service.py`, `legacy_import_service.py`, `order_service.py`, `pick_wave_service.py`, and `manapool_service.py`. Runtime data is stored in `cardfoundry.db`; treat it as local state, not source code. There is currently no dedicated tests or assets directory.

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

The repository does not yet include automated tests. For new behavior, add focused `pytest` tests under `tests/`, using names such as `test_order_service.py` and `test_allocate_order_shortage`. Run them with `pytest`. Isolate tests from `cardfoundry.db` by using a temporary SQLite database, and mock Mana Pool or Scryfall HTTP requests. At minimum, run the syntax check and manually exercise affected FastAPI routes before submitting.

## Commit & Pull Request Guidelines

Recent commits use short, imperative summaries, often versioned, for example `CardFoundry v0.0.16 - Add cost basis...`. Keep commits narrowly scoped and describe the user-visible outcome. Pull requests should include a concise summary, verification steps, linked issues when applicable, and screenshots for HTML/UI changes. Call out schema changes and migration implications explicitly.

## Security & Configuration

Configure `MANAPOOL_EMAIL` and `MANAPOOL_API_TOKEN` through a local `.env` file. Never commit credentials, customer exports, or `cardfoundry.db`. Avoid tests that call production marketplace APIs or mutate live inventory.
