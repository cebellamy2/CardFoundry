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

### Cards waiting for a price

A card can be imported with the asking price left blank. It commits to
inventory as normal, but it is **held out of new listings** until someone
prices it &mdash; CardFoundry will not invent a $0.00 asking price. A held card
is inventory that cannot be sold.

Held cards appear in two places:

- **Attention** &mdash; as a *Needs price* item, counted in the nav badge. It
  reads *Worth a look* for the first week and *Needs action* from seven days
  held onward, and it shows how many days the card has been waiting. The link
  goes straight to the Set price form.
- **Inventory Sync &rarr; Exceptions to Review** &mdash; the *Needs price*
  table, with the same Set price button.

Setting a price clears the hold and the item disappears on its own; there is
nothing to dismiss. For a card in a consignment batch, setting the price here
also records it as that card's consignment value.

## Attention

**Attention** is the single list of everything waiting on you, with a count in
the top navigation. Each row is either **Needs action** or **Worth a look**, and
each links straight to the screen that resolves it.

Anything you have consciously judged can be **dismissed** with a reason. A
dismissal silences that *situation*, not the item forever: if the situation gets
worse, the item comes back and your note stays as the record of what you decided
about the previous state. Dismissals are listed under **Set aside**, with
un-dismiss.

What appears there:

| Category | What it means |
| --- | --- |
| Mana Pool sync | An order's shipped/processing status has not reached Mana Pool. |
| **Late order** | An order is approaching, or past, its Mana Pool shipping deadline. |
| **Order not received** | Mana Pool has an order that does not exist in CardFoundry. |
| Short order | An order could not be fully allocated. |
| **Needs price** | A card is held out of listings because it has no asking price. |
| Fulfillment exception | A pick problem is unresolved. |
| Webhook delivery | An order Mana Pool pushed has not been ingested. |
| Listing drift | A Mana Pool listing disagrees with local inventory. |
| Pricing freshness | The pricing cron has not run recently, or priced almost nothing. |
| Price jump | A card's price moved a long way. |

### Late order

Mana Pool expects an order to ship within **two business days** of the order
date. CardFoundry now stores the order's real date from Mana Pool (**Order
placed**, shown separately from **Ingested**, which is when CardFoundry first saw
it) and works out a **Ship by** deadline from it. That deadline appears on the
Orders list, the order page and the pick wave.

The alert reads *Worth a look* about a day out and **Needs action** within about
twelve hours or once it is overdue, saying plainly how long is left or how late
it already is. Shipping the order clears it; there is nothing to dismiss.

Weekends never count toward the two days. The deadline is worked out
deliberately on the cautious side &mdash; the order's date is read as Mana Pool
shows it, and the deadline day ends at midnight Eastern &mdash; so the warning
can arrive early but never late. The number of business days and both warning
thresholds are settings, so they can be corrected without a release.

An order with no recorded **Order placed** date is *not* guessed at: it is left
out of the alert rather than given a deadline measured from when CardFoundry
happened to see it.

### Order not received

This one is different from every other row on the page, because it is not about
an order CardFoundry knows about &mdash; it is about one it *doesn't*.

Every other alert here starts from an order record. So does the shipping
deadline. That means an order Mana Pool is holding us to, but which never
arrived in CardFoundry, has no deadline, no status and no page: it is invisible
to the whole rest of this list. That is not theoretical. One order in September
2026 reached us four days late, went unshipped for about six days, and the
seller account was restricted &mdash; and no alert could have caught it, because
for most of that time there was no order here to measure.

The hourly order sync now compares Mana Pool's list of orders awaiting shipment
against what exists locally, and anything on their side with nothing on ours is
raised here as **Needs action**, with the Mana Pool order number, the date the
order was placed, and the reason the sync gave if it reported one. Look the order
up on Mana Pool by that number.

It clears itself. The next hourly sync that finds the order present and
error-free resolves the row without anyone touching it, so a problem that heals
stops asking for attention. Dismissing one is possible but deliberately
short-lived: if a *second* order goes missing, the alert returns for both, because
one missing order is a glitch and two is a pattern.

This check costs no extra Mana Pool requests &mdash; it reuses the list the sync
already fetches &mdash; and the page and the badge both read it from local
records, so no page load waits on Mana Pool.

### Cards waiting for a price

See **Pricing &rarr; Cards waiting for a price** above.

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
