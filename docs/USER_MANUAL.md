# Operator User Manual

## Start CardFoundry

From the repository:

```bash
source .venv/bin/activate
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>. The navigation links to Batches, Inventory,
Orders, Pick Waves, Price Updates, Inventory Sync, Legacy Migration, Go-Live,
and Import History.

## Dashboard and inventory

The dashboard shows batches and clearly labeled inventory counts. Total Owned
is `available + unsellable + reserved`; sold and removed records remain history
but are not owned.

Use **Inventory Search** (`/inventory`) to search cards and filter status:

- **available** — owned and sellable
- **unsellable / NOT FOR SALE** — owned but excluded from sale
- **reserved** — committed to an order
- **sold** — no longer owned
- **removed** — retained correction record that was not a physical owned copy

Open a card to view identity, batch, pricing/cost fields, controls, and history.

## Import a production batch

On the home page, use **Production Batch Import**:

1. Enter the intentional batch name and source/location.
2. Upload the CSV and select **Preview Production Import**.
3. Review filename, detected columns, CSV rows, physical cards, canonical and
   bound counts, duplicates, warnings, missing prices, and expected inventory.
4. Resolve any blank source prices on the preview when prompted.
5. Select **Confirm Atomic Production Import** only after review.

Preview stores a `PendingImport` but creates no Batch or InventoryCard. Confirm
revalidates source and evidence, then atomically creates the production Batch,
ImportRecord, cards, bindings, and audit. Quantity values expand to one
InventoryCard per physical copy. Missing language defaults to EN; explicit
languages are preserved.

If validation reports ambiguous, unresolved, conflicting, missing printing, or
finish evidence, correct the source or reviewed metadata and create a new
preview. Do not force an identity.

## Correct a card

The normal edit page supports local metadata and cost corrections for eligible
cards. For a wrong printing, choose **Select Correct Printing**, review the
Scryfall-backed printing choices, then confirm the exact printing. Correction
is local and audited; it does not publish automatically.

## Not For Sale

On an available card select **Mark Not For Sale**, choose a reason, optionally
add a note, review, and confirm. Reasons include personal use, damaged, trade,
display, hold, and other. The card remains in its original Batch and Total
Owned but contributes zero sellable quantity.

An unsellable card exposes **Return to Sellable Inventory**. Return is refused
for archived batches, active allocations, or invalid workflow state. Neither
action contacts Mana Pool; run a separately reviewed synchronization later.

## Local sale, trade, or gift

Use **Mark Sold / Traded Locally** on an available card. Choose local sale,
trade, gift, or other and enter the required transaction note. Optional value
and trade-receipt details are retained. Confirmation changes the card to sold,
preserves Batch/cost/history, and removes it from sellable quantity. Incoming
trade cards must use the normal production import workflow.

## Remove an erroneous inventory record

Use **Remove From Inventory** only when a record never represented an
additional physical card—for example, a duplicate scan. Select a structured
reason, enter the required note, and optionally identify the surviving related
InventoryCard. Review the warning and confirm. The status becomes `removed`;
the row and original Batch remain for audit.

For an existing removed record, **Correct Removal Details** can amend its
reason, note, or related card. It adds a new audit event and never rewrites the
original removal event.

## Pricing

The Price Updates page creates previews before Apply. Pricing selects the
lowest qualifying seller-excluded competitor across all languages, otherwise
an exact-printing/finish market price, otherwise a reviewed manual initial
price, otherwise HOLD. Language remains strict for the listing itself.

No positive sellable listing may be below **$0.65**. The floor applies to
existing, competitor, market, and manual sources. A below-floor existing price
is explicitly corrected rather than preserved.

**Set Manual Initial Price** appears only for an exact, validated net-new
variant held because both automatic sources are absent. Enter the human price
and required note, then type `SET MANUAL INITIAL PRICE`. This saves local
evidence only; it does not contact Mana Pool.

HOLD means CardFoundry lacks safe evidence. Resolve the evidence or create an
eligible reviewed manual fallback; never invent identity or an above-floor
price.

## Inventory Sync and rebuilds

Inventory Sync previews compare CardFoundry availability with authoritative
seller inventory. Preview is read-only, although it may ingest orders locally.

> **WARNING — MARKETPLACE WRITES:** Clean rebuild, inventory Apply, pricing
> Apply, and floor-correction execution can write to Mana Pool. A full rebuild
> is store-off maintenance only. Never use a historical preview, never bypass
> typed confirmation, and never rerun a partial execution from the beginning.

A clean rebuild uses a structural preview, a fresh execution-pricing seal, an
exact confirmation, and durable checkpoints. If recovery is required, open the
execution recovery page and resume that exact execution. Do not start another.

Local-only actions include import preview/commit, printing correction,
sellability changes, manual disposition, removal, removal-metadata correction,
and saving manual fallback evidence. Mana Pool reads occur during import
validation, pricing preview, sync preview, and reconciliation; explicit Apply
or execution actions are the write boundary.
