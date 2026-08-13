# Development Guide

## Prerequisites and setup

The verified environment uses Python 3.13 and SQLite. A minimum supported
Python version is not declared in project metadata; treat that as a TODO before
packaging for other environments.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create an untracked `.env` containing configuration names as needed:

```text
MANAPOOL_EMAIL=...
MANAPOOL_API_TOKEN=...
```

Never put real values in documentation, tests, fixtures, or commits.

## Database and application

`database.py` uses `sqlite:///./cardfoundry.db`. Importing `models.py` creates
missing tables; `upgrade_existing_database()` performs the project's existing
additive-column and guarded SQLite table-rebuild upgrades. This is a lightweight
migration pattern, not a versioned migration framework. For every schema change:

1. Update the SQLAlchemy model.
2. Add a safe, idempotent upgrade in `database.py` if existing databases need
   it.
3. Update production-reset classification for new tables.
4. Test fresh and upgraded temporary databases.

Start development:

```bash
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>.

## Verification

```bash
PYTHONPATH=. pytest -q
PYTHONPATH=. python -m compileall -q . -x '/\.git/|/\.venv/'
PYTHONPATH=. python -c 'import main'
sqlite3 cardfoundry.db 'PRAGMA integrity_check;'
git diff --check
```

Tests must use temporary SQLite databases and fake/mock external calls. No test
may call production Mana Pool write endpoints.

## Repository layout

- `main.py` — FastAPI routes and server-rendered UI
- `models.py`, `database.py` — persistence and upgrades
- `production_import_service.py` — canonical reviewed batch import
- `inventory_*`, `order_service.py`, `pick_wave_service.py` — inventory/order
  workflows
- `pricing_*`, `competitor_pricing_service.py`,
  `new_listing_pricing_service.py` — pricing policy and evidence
- `clean_rebuild_*`, `execution_pricing_*` — structural preview, seal,
  execution, and recovery
- `sellability_service.py`, `printing_correction_service.py` — local card
  lifecycle and identity corrections
- `manapool_service.py` — external API boundary
- `tests/` — focused unit/integration tests with fakes
- `audits/` — sanitized immutable production summaries

## Adding behavior

Keep route parsing/rendering in `main.py`; put reusable business rules in a
service. Make state transitions explicit and atomic. Use append-only audit
events. Preserve Batch/ImportRecord provenance. Use `InventorySyncLease` for
inventory-affecting operations and protect stale reviewed state with hashes.

For Mana Pool integrations:

- Separate reads from writes visibly.
- Use seller inventory for authoritative write/readback reconciliation.
- Require exact printing, product, language, condition, and finish identity.
- Treat timeouts after writes as uncertain and reconcile before retry.
- Require store-off verification for destructive maintenance.
- Inject clients into tests; never use live credentials.

## Production safety rules

- CardFoundry is authoritative for physical sellable quantity.
- Only `available` cards in active batches are publishable/allocatable.
- Never infer identity or product IDs.
- Never preserve or apply a positive price below the configured floor.
- Preview, approval, enablement, execution, and recovery are separate gates.
- Never commit `.env`, databases, backups, incoming operator exports, or raw API
  responses containing customer or credential data.

TODO: add packaged configuration, supported-Python metadata, a formal migration
framework, deployment/service-unit instructions, and CI configuration.
