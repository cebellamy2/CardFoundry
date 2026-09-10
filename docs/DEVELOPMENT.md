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

### Job retention (inventory_sync_jobs / pricing_jobs JSON)

Every inventory-sync and pricing job stores a full JSON blob. Three types
(`maintenance_preview`, `competitor_only_full_preview`, `clean_rebuild_preview`)
are 7-9 MB each and were 97% of a 1.45 GB production database. A daily sweep
(`scheduled_job_retention.py` → `POST /admin/job-retention/sweep`, also on the
/admin "Job Retention" card) replaces the blob on rows older than the retention
window (14 days, overridable via the `job_retention_days` app setting) with a
compact summary. Rows are never deleted; see `job_retention_service.py` for
exactly what each type keeps. Trimmed jobs still list in history (marked
"trimmed <date>") and open to a summary page; deriving or applying from a
trimmed preview is refused with a 409.

**First run (deliberate, manual):**

1. Confirm a recent Railway backup exists.
2. On `/admin`, click **Preview Sweep (dry run)** and check the counts and MB.
3. Click **Run Sweep Now**.
4. The freed pages stay inside the SQLite file until `VACUUM` runs. In a quiet
   window (between cron ticks -- `VACUUM` takes the write lock for tens of
   seconds at this size and needs roughly the file's own size free on the
   volume):

   ```bash
   railway ssh -s CardFoundry -e production -- sh -c \
     "sqlite3 /data/cardfoundry.db 'PRAGMA page_count; VACUUM; PRAGMA page_count;'"
   ```

5. Open one trimmed job from Preview History and confirm the summary page.

Routine sweeps after that need no `VACUUM`; freed pages are reused and the
file stops growing.

### Browser tests (chute detection)

`tests/test_chute_detection_playwright.py` drives the chute's real client-side
detection JS in headless Chromium, feeding a generated clip to `getUserMedia()`
through Chromium's fake-camera flags (see `tests/chute_fake_camera.py`). It
needs Playwright's Chromium build, a one-time ~150MB download:

```bash
PYTHONPATH=. playwright install chromium
```

Without it the browser tests skip rather than fail. They run in real time
(roughly the clip length, ~35s each) because the page's detection loop cannot
be fast-forwarded.

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
