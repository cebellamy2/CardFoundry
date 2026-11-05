# Repository Guidelines

> **Coding standards, non-negotiable rules and architectural judgement live
> in [CLAUDE.md](CLAUDE.md).** Read that first. This file is the mechanical
> contract: setup, commands, commit format and the release procedure.

## Project Structure & Module Organization

CardFoundry is a FastAPI application organized as flat Python modules. `main.py` defines the web application, routes, and server-rendered HTML. `models.py` contains SQLAlchemy models, while `database.py` configures the local SQLite database and lightweight schema upgrades. Domain logic is separated into focused import, inventory, pricing, order, and maintenance service modules. Runtime data is stored in `cardfoundry.db`; treat it as local state, not source code. Automated tests live under `tests/`, and sanitized immutable production summaries live under `audits/`.

## Build, Test, and Development Commands

Create and activate a virtual environment before installing dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the development server with `uvicorn main:app --reload`; the application is then available at `http://127.0.0.1:8000`. Use `python -m compileall .` for a quick syntax check. No build step is required.

Install the pre-push deploy guard once per clone:

```bash
git config core.hooksPath scripts/hooks
```

## Coding Style & Naming Conventions

Python 3 type hints and four-space indentation. `snake_case` for functions and variables, `PascalCase` for SQLAlchemy models, uppercase for module constants. Prefer small functions, explicit SQLAlchemy queries, and descriptive status values. No formatter or linter is configured, so keep changes consistent with nearby code and PEP 8. See CLAUDE.md for where new code belongs.

## Testing Commands

Add focused `pytest` tests under `tests/`, using names such as `test_order_service.py` and `test_allocate_order_shortage`. Run them with `PYTHONPATH=. pytest -q`. Isolate tests from `cardfoundry.db` by using a temporary SQLite database, and mock Mana Pool or Scryfall HTTP requests. No automated test may call a live marketplace write endpoint.

Before submitting production changes, run at minimum:

```bash
PYTHONPATH=. pytest -q
PYTHONPATH=. python -m compileall -q . -x '/\.git/|/\.venv/'
sqlite3 cardfoundry.db 'PRAGMA integrity_check;'
git diff --check
```

See CLAUDE.md for what a test should assert.

## Commit & Pull Request Guidelines

Commits use short, imperative, conventional-commit-style summaries (`feat: ...`, `fix: ...`, `chore: ...`, `test: ...`). Keep commits narrowly scoped and describe the user-visible outcome. Pull requests should include a concise summary, verification steps, linked issues when applicable, and screenshots for HTML/UI changes. Call out schema changes and migration implications explicitly.

## Versioning & Releases

CardFoundry uses real [Semantic Versioning](https://semver.org/) starting at `1.0.0` (the verified production go-live baseline, commit `77562a9`). Every commit shipped to `main` is a release and gets its own version: `feat:` bumps MINOR, `fix:`/`test:`/`chore:` bump PATCH, a breaking change bumps MAJOR. As part of each release commit:

1. Update the `VERSION` file (single line, e.g. `1.38.0`) to the new version.
2. Add an entry to `CHANGELOG.md` (Keep a Changelog format) under that version.
3. After pushing, tag the commit `vMAJOR.MINOR.PATCH` and push the tag.

The current version is read from `VERSION` at import time (`main.APP_VERSION`) and shown in the footer of every page -- confirm it updated after a deploy rather than assuming.

## Security & Configuration

Configure `MANAPOOL_EMAIL` and `MANAPOOL_API_TOKEN` through a local `.env` file. Never commit credentials, customer exports, or `cardfoundry.db`.

## Unattended Helpers

Two macOS LaunchAgents can build code with nobody at the keyboard. Neither can push, tag, deploy or touch Railway; each leaves a local branch, and merging one to `main` is an ordinary release. Both obey the `STOP` and `PAUSE` sentinels and share one lock, so only one runs at a time. Ticket and approval formats are in [scripts/autodev/TICKET-FORMAT.md](scripts/autodev/TICKET-FORMAT.md); the full design is in `scripts/autodev/README.md`.

- **AutoDev** (`com.cardfoundry.autodev.serve`, every 5 minutes) works approved tickets from `claude/autodev/queue/` onto `autodev/<task-id>` branches, fenced by `scripts/autodev/policy.py`. It does nothing unless `claude/autodev/config.json` says `"scheduling_enabled": true`; set that key by hand, since no command writes it.
- **The inbox runner** (`com.cardfoundry.autodev.inbox`, every 20 minutes) works one ticket file at a time from `claude/mailbox/to-code-autodev/` onto an `inbox/<ticket-id>` branch, fenced by the ticket's own `files` list, and files its report in `claude/mailbox/from-code-autodev/`. It ships disabled (`inbox_enabled` is false).

The install, enable, disable and uninstall commands are dry runs until you add `--confirm`:

```bash
# AutoDev: install the agent, check it, remove it
PYTHONPATH=. .venv/bin/python -m scripts.autodev.install_autoserve --confirm
PYTHONPATH=. .venv/bin/python -m scripts.autodev.install_autoserve --status
PYTHONPATH=. .venv/bin/python -m scripts.autodev.install_autoserve --uninstall --confirm

# Inbox runner: install and turn on, check it, turn off (agent stays), remove it
PYTHONPATH=. .venv/bin/python -m scripts.autodev.install_inbox --enable --confirm
PYTHONPATH=. .venv/bin/python -m scripts.autodev.install_inbox --status
PYTHONPATH=. .venv/bin/python -m scripts.autodev.install_inbox --disable --confirm
PYTHONPATH=. .venv/bin/python -m scripts.autodev.install_inbox --uninstall --confirm

# Both at once: pause starts nothing new, stop halts after the current phase;
# resume clears PAUSE, and --clear-stop clears STOP too
PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator pause
PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator stop
PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator resume --clear-stop
```
