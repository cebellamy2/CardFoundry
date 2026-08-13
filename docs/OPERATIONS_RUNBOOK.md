# Operations Runbook

## Safety rule

**FAIL CLOSED when identity, quantity, price evidence, store state, or execution
state is uncertain.** Keep the Mana Pool singles store OFF during destructive
maintenance. Never infer a product ID, replay an uncertain batch blindly, or
replace an approved plan midway through recovery.

## Application will not start

1. Activate `.venv` and run `python -m py_compile main.py`.
2. Confirm dependencies: `pip install -r requirements.txt`.
3. Confirm `.env` exists locally and required credential names are configured.
4. Run `sqlite3 cardfoundry.db 'PRAGMA integrity_check;'`.
5. Do not delete the database. Restore from a verified backup if integrity is
   not `ok`.

## Backup and integrity

Before a production reset use the guarded reset command; it creates a
timestamped SQLite backup and verifies it before clearing data:

```bash
.venv/bin/python production_reset.py --dry-run
```

Do not execute a reset during ordinary operations. For a manual safety copy,
stop write activity and use SQLite's backup facility rather than copying a
database with active writes. Backups belong under ignored `backups/`.

## Import problems

- **Import refused:** Read the exact preview error; no Batch or cards should
  exist. Fix the CSV or reviewed inputs and preview again.
- **Ambiguous/unresolved printing:** Verify Scryfall identity, set, collector,
  language, condition, and finish. Never choose a nearby product ID.
- **Wrong printing after import:** Use **Select Correct Printing** on the card.
- **Duplicate physical record:** Use **Remove From Inventory** on the erroneous
  available record; link the surviving card when applicable.
- **Stale preview/evidence:** Create a new preview. Do not confirm the old one.

## Inventory leaves or is withheld

- Personal use/display/damage: **Mark Not For Sale**.
- Local sale/trade/gift: **Mark Sold / Traded Locally** with required note.
- Erroneous record that was never another owned copy: **Remove From Inventory**.
- Incoming trade cards: import them as a new production batch; do not create
  them from the outgoing note.

These actions are local. Follow with a reviewed inventory reconciliation when
remote desired quantity must change.

## Pricing problems

- **HOLD / no competitor:** The engine tries exact-printing/finish market data.
  If both are absent and identity is exact, save a reviewed manual initial
  price. Automatic evidence wins when later available.
- **Price below $0.65:** Keep the store OFF and generate a dedicated floor-
  correction preview. Review exact quantities and targets. Do not use the
  rebuild executor for price-only correction.
- **Unexpected price movement:** Generate a new pricing preview. Execution seal
  movement beyond $1.00 or 20% requires a human review note.

## Quantity mismatch

1. Do not write immediately; determine whether CardFoundry status, active Batch,
   order/allocation state, or seller inventory changed.
2. Use authoritative `/seller/inventory`, not buyer listings, for immediate
   state.
3. Reconcile/import orders and create a fresh preview.
4. If structure changed, historical previews and seals are invalid.

## Lease conflict

An active `InventorySyncLease` means another inventory operation is running.
Do not delete a live lease. If a process crashed, verify no operation remains,
inspect expiry and execution journals, and recover the journaled operation.
Active or `recovery_required` rebuild/floor executions intentionally block
ordinary inventory mutations.

## Partial Mana Pool operation

### Clean rebuild

Keep the store OFF. Open
`/inventory-sync/rebuild-executions/{execution_id}`. Inspect the recovery report
and use the guarded resume for the same execution. Completed checkpoints are
read back and skipped; sealed prices are reused. Never create a replacement
preview after writes have begun unless recovery explicitly concludes it cannot
continue and the full remote state is reviewed.

### Floor correction

Keep the store OFF and resume the exact execution from the command line:

```bash
PYTHONPATH=. .venv/bin/python floor_correction_execute.py \
  --resume-execution EXECUTION_ID \
  --confirmation "STORE IS OFF - APPLY PRICING FLOOR CORRECTION"
```

The executor rechecks store state, local and aggregate quantities, then writes
only remaining incorrect targets. A timeout whose write succeeded is marked
complete from readback; partial uncertainty enters `recovery_required`.

## When a new preview is mandatory

Create a new preview when inventory identity/status/quantity, Batch archive
state, orders/allocations, pending imports, bindings/product IDs, seller
quantity structure, manual evidence, or pricing policy changed. Ordinary
automated market movement may be refreshed only through the execution-pricing
seal guardrail workflow before destructive writes begin.

## Store and post-operation verification

Read `/account` and positively verify `singles_live` rather than assuming it.
After any write, fetch complete seller inventory and verify exact product
quantities, reviewed prices, unexpected positives, aggregate quantity, and the
expected state hash. Confirm the journal is completed and no recovery remains.
Only a human turns the store on.
